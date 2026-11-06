"""Safe subprocess wrapper for the external :command:`arctic` CLI.

The wrapper deliberately exposes only bounded subreddit acquisition and
bounded, read-only JSONL queries.  Arctic's ``-n`` flag limits query output; it
does *not* limit ``arctic sub`` downloads.  Acquisition therefore always uses
the API path, a short date window, a project-local data directory, one worker,
and an active disk guard.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TextIO
from uuid import uuid4


class ArcticError(RuntimeError):
    """Base error for Arctic integration failures."""


class UnsafeArcticCommand(ArcticError):
    """Raised when an unbounded or mutating Arctic command is requested."""


class AcquisitionGuardError(ArcticError):
    """Raised when a bounded acquisition cannot be planned safely."""


class StorageLimitExceeded(AcquisitionGuardError):
    """Raised when an active acquisition crosses a storage safety boundary."""


class ArcticOutputError(ArcticError):
    """Raised when Arctic emits malformed JSON/JSONL."""


class Route(StrEnum):
    """Acquisition route reported or enforced for an Arctic command."""

    API = "api"
    TORRENT = "torrent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured result and durable logs for one Arctic invocation."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    stdout_log: Path
    stderr_log: Path
    route: Route = Route.UNKNOWN
    stopped_reason: str = ""


class ArcticCommandError(ArcticError):
    """Raised for a non-successful Arctic process."""

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        detail = result.stderr.strip().splitlines()
        summary = detail[-1] if detail else "geen fouttekst ontvangen"
        super().__init__(f"Arctic stopte met exitcode {result.returncode}: {summary}")


@dataclass(frozen=True, slots=True)
class AcquisitionPlan:
    """Validated, bounded acquisition invocation."""

    subreddit: str
    after: str
    before: str
    kind: str
    max_bytes: int
    min_free_bytes: int
    max_window_days: int
    timeout_seconds: int
    process_timeout_seconds: int
    data_dir: Path
    bytes_in_use: int
    free_bytes: int
    allowed_additional_bytes: int
    command: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation for CLI previews."""

        value = asdict(self)
        value["data_dir"] = str(self.data_dir)
        value["command"] = list(self.command)
        return value


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Installation and runtime readiness report."""

    ok: bool
    binary: str
    resolved_binary: str | None
    version: dict[str, Any] | None
    info: dict[str, Any] | None
    issues: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProcessLike(Protocol):
    """Small Popen surface used by the wrapper and test doubles."""

    stdout: TextIO | None
    stderr: TextIO | None
    returncode: int | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


PopenFactory = Callable[..., ProcessLike]
DiskUsage = Callable[[str | os.PathLike[str]], Any]
Which = Callable[[str], str | None]

_SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
_KINDS = frozenset({"both", "comments", "submissions"})
_FORBIDDEN_COMMANDS = frozenset({"pull", "publish", "process", "sub", "user", "query"})
_READ_ONLY_COMMANDS = frozenset({"info", "version", "stats", "catalog"})


class ArcticCLI:
    """Run Arctic with conservative, cross-platform safety boundaries."""

    def __init__(
        self,
        *,
        binary: str = "arctic",
        data_dir: Path | str | None = None,
        logs_dir: Path | str | None = None,
        popen_factory: PopenFactory = subprocess.Popen,
        disk_usage: DiskUsage = shutil.disk_usage,
        which: Which = shutil.which,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        poll_interval: float = 0.25,
    ) -> None:
        self.binary = binary
        self.data_dir = Path(data_dir).resolve() if data_dir is not None else None
        default_logs = self.data_dir.parent / "logs" if self.data_dir else Path.cwd() / "logs"
        self.logs_dir = Path(logs_dir).resolve() if logs_dir is not None else default_logs
        self._popen = popen_factory
        self._disk_usage = disk_usage
        self._which = which
        self._sleep = sleep
        self._monotonic = monotonic
        self._poll_interval = max(0.01, poll_interval)
        self.last_result: CommandResult | None = None

    def command(
        self,
        args: Sequence[str] | str,
        *more_args: str,
        check: bool = True,
        timeout_seconds: int = 120,
    ) -> CommandResult:
        """Run a small read-only Arctic command.

        Acquisition, query, bulk pull, processing, user collection, and publish
        are intentionally rejected here.  Use :meth:`acquire_subreddit` and
        :meth:`query_jsonl` for the two supported data operations.
        """

        tokens = self._coerce_args(args, more_args)
        if not tokens:
            raise UnsafeArcticCommand("Een Arctic-subcommando is verplicht.")
        if "--help" in tokens or "-h" in tokens:
            return self._execute(tokens, label="help", check=check, timeout_seconds=timeout_seconds)
        forbidden = _FORBIDDEN_COMMANDS.intersection(tokens)
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise UnsafeArcticCommand(f"Gebruik de begrensde wrapper; geweigerd: {names}.")
        root = next((token for token in tokens if not token.startswith("-")), "")
        if root not in _READ_ONLY_COMMANDS:
            raise UnsafeArcticCommand(f"Arctic-commando is niet toegestaan: {root or tokens[0]}")
        if root == "catalog" and ("refresh" in tokens or "--sizes" in tokens):
            raise UnsafeArcticCommand("catalog refresh/--sizes schrijft of gebruikt netwerkcache.")
        return self._execute(tokens, label=root, check=check, timeout_seconds=timeout_seconds)

    def help(self, command: str | None = None) -> CommandResult:
        """Return Arctic's top-level or subcommand help without executing it."""

        if command is None:
            args = ("--help",)
        else:
            if not re.fullmatch(r"[a-z][a-z-]*", command):
                raise UnsafeArcticCommand("Ongeldige helpnaam.")
            args = (command, "--help")
        return self._execute(args, label="help", check=True, timeout_seconds=30)

    def info(self) -> dict[str, Any]:
        """Return the first structured row from ``arctic info -o json``."""

        result = self.command(("info", "-o", "json"), timeout_seconds=60)
        return self._parse_json_object(result.stdout, "arctic info")

    def doctor(self) -> DoctorReport:
        """Check executable discovery, version output, and hardware info."""

        resolved = self._which(self.binary)
        if resolved is None and Path(self.binary).is_file():
            resolved = str(Path(self.binary).resolve())
        if resolved is None:
            return DoctorReport(
                ok=False,
                binary=self.binary,
                resolved_binary=None,
                version=None,
                info=None,
                issues=(
                    "Arctic CLI is niet gevonden op PATH. Installeer de officiële arctic-binary.",
                ),
            )

        issues: list[str] = []
        version: dict[str, Any] | None = None
        info: dict[str, Any] | None = None
        try:
            version_result = self.command(("version", "-o", "json"), timeout_seconds=30)
            version = self._parse_json_object(version_result.stdout, "arctic version")
        except ArcticError as exc:
            issues.append(str(exc))
        try:
            info = self.info()
        except ArcticError as exc:
            issues.append(str(exc))
        return DoctorReport(
            ok=not issues,
            binary=self.binary,
            resolved_binary=resolved,
            version=version,
            info=info,
            issues=tuple(issues),
        )

    def plan_acquisition(
        self,
        subreddit: str,
        *,
        after: str | date | None,
        before: str | date | None,
        max_bytes: int | None,
        min_free_bytes: int | None,
        kind: str = "both",
        max_window_days: int = 31,
        timeout_seconds: int = 60,
        process_timeout_seconds: int = 3_600,
    ) -> AcquisitionPlan:
        """Validate and preview a bounded API-only acquisition.

        The maximum is an absolute cap for the complete Arctic project data
        directory.  An active run is terminated if it crosses this cap or the
        requested free-space reserve.
        """

        data_dir = self._require_data_dir()
        name = self._normalize_subreddit(subreddit)
        if kind not in _KINDS:
            raise AcquisitionGuardError("kind moet both, comments of submissions zijn.")
        if max_bytes is None or max_bytes <= 0:
            raise AcquisitionGuardError("Een positieve --max-bytes is verplicht.")
        if min_free_bytes is None or min_free_bytes <= 0:
            raise AcquisitionGuardError("Een positieve vrije-ruimtereserve is verplicht.")
        if max_window_days <= 0:
            raise AcquisitionGuardError("max_window_days moet positief zijn.")
        if timeout_seconds <= 0 or process_timeout_seconds <= 0:
            raise AcquisitionGuardError("Timeouts moeten positief zijn.")

        after_date = self._require_date(after, "after")
        before_date = self._require_date(before, "before")
        span = (before_date - after_date).days
        if span <= 0:
            raise AcquisitionGuardError("before moet na after liggen.")
        if span > max_window_days:
            raise AcquisitionGuardError(
                f"Datumvenster is {span} dagen; maximum is {max_window_days} dagen."
            )

        data_dir.mkdir(parents=True, exist_ok=True)
        bytes_in_use = self._directory_size(data_dir)
        if bytes_in_use >= max_bytes:
            raise AcquisitionGuardError(
                f"Datalimiet al bereikt ({bytes_in_use} van {max_bytes} bytes)."
            )
        free_bytes = int(self._disk_usage(data_dir).free)
        allowed = max_bytes - bytes_in_use
        required = min_free_bytes + allowed
        if free_bytes < required:
            raise AcquisitionGuardError(
                f"Onvoldoende vrije ruimte: {free_bytes} beschikbaar, {required} vereist."
            )

        after_text = after_date.isoformat()
        before_text = before_date.isoformat()
        command = (
            self.binary,
            "--data-dir",
            str(data_dir),
            "--workers",
            "1",
            "--timeout",
            f"{timeout_seconds}s",
            "sub",
            name,
            "--api",
            "--after",
            after_text,
            "--before",
            before_text,
            "--kind",
            kind,
        )
        return AcquisitionPlan(
            subreddit=name,
            after=after_text,
            before=before_text,
            kind=kind,
            max_bytes=max_bytes,
            min_free_bytes=min_free_bytes,
            max_window_days=max_window_days,
            timeout_seconds=timeout_seconds,
            process_timeout_seconds=process_timeout_seconds,
            data_dir=data_dir,
            bytes_in_use=bytes_in_use,
            free_bytes=free_bytes,
            allowed_additional_bytes=allowed,
            command=command,
        )

    def acquire_subreddit(
        self,
        subreddit: str,
        *,
        after: str | date | None,
        before: str | date | None,
        max_bytes: int | None,
        min_free_bytes: int | None,
        kind: str = "both",
        max_window_days: int = 31,
        timeout_seconds: int = 60,
        process_timeout_seconds: int = 3_600,
    ) -> CommandResult:
        """Execute one validated API-only subreddit acquisition."""

        plan = self.plan_acquisition(
            subreddit,
            after=after,
            before=before,
            max_bytes=max_bytes,
            min_free_bytes=min_free_bytes,
            kind=kind,
            max_window_days=max_window_days,
            timeout_seconds=timeout_seconds,
            process_timeout_seconds=process_timeout_seconds,
        )
        # The full command was produced internally after validation.  It never
        # selects torrent, pull, process, user, or publish.
        return self._execute(
            plan.command[1:],
            label=f"sub-{plan.subreddit}",
            check=True,
            timeout_seconds=plan.process_timeout_seconds,
            absolute_command=plan.command,
            storage_guard=plan,
        )

    def query_jsonl(
        self,
        subreddit: str,
        *,
        limit: int,
        kind: str = "both",
        contains: str | None = None,
        after: str | date | None = None,
        before: str | date | None = None,
        timeout_seconds: int = 300,
        allow_unlimited: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Stream at most ``limit`` imported records as parsed JSON objects."""

        data_dir = self._require_data_dir()
        name = self._normalize_subreddit(subreddit)
        if limit < 0 or (limit == 0 and not allow_unlimited):
            raise AcquisitionGuardError(
                "query limit moet groter dan nul zijn; gebruik allow_unlimited bewust voor lokaal streamen."
            )
        if kind not in _KINDS:
            raise AcquisitionGuardError("kind moet both, comments of submissions zijn.")
        if timeout_seconds <= 0:
            raise AcquisitionGuardError("Timeout moet positief zijn.")

        args: list[str] = [
            self.binary,
            "--data-dir",
            str(data_dir),
            "--workers",
            "1",
            "--timeout",
            "60s",
            "query",
            name,
            "--kind",
            kind,
            "-n",
            str(limit),
            "-o",
            "jsonl",
        ]
        if contains:
            args.extend(("--contains", contains))
        if after is not None:
            args.extend(("--after", self._require_date(after, "after").isoformat()))
        if before is not None:
            args.extend(("--before", self._require_date(before, "before").isoformat()))
        yield from self._stream_jsonl(
            tuple(args), label=f"query-{name}", limit=limit, timeout=timeout_seconds
        )

    @staticmethod
    def detect_route(stderr: str, command: Sequence[str] = ()) -> Route:
        """Detect the acquisition path from command flags and Arctic stderr."""

        if "--api" in command:
            return Route.API
        lowered = stderr.casefold()
        if "using the api" in lowered or "arctic shift api" in lowered:
            return Route.API
        if "torrent bundle" in lowered or "from the torrent" in lowered:
            return Route.TORRENT
        return Route.UNKNOWN

    def _execute(
        self,
        args: Sequence[str],
        *,
        label: str,
        check: bool,
        timeout_seconds: int,
        absolute_command: Sequence[str] | None = None,
        storage_guard: AcquisitionPlan | None = None,
    ) -> CommandResult:
        command = tuple(absolute_command or (self.binary, *self._data_args(), *args))
        stdout_path, stderr_path = self._new_log_paths(label)
        process = self._start_process(command)
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        stopped_reason = ""
        started = self._monotonic()

        with (
            stdout_path.open("w", encoding="utf-8", newline="") as stdout_log,
            stderr_path.open("w", encoding="utf-8", newline="") as stderr_log,
        ):
            threads = (
                self._pump_thread(process.stdout, stdout_log, stdout_parts),
                self._pump_thread(process.stderr, stderr_log, stderr_parts),
            )
            while process.poll() is None:
                if self._monotonic() - started > timeout_seconds:
                    stopped_reason = f"proces-timeout na {timeout_seconds} seconden"
                    self._stop_process(process)
                    break
                if storage_guard is not None:
                    used = self._directory_size(storage_guard.data_dir)
                    if used > storage_guard.max_bytes:
                        stopped_reason = (
                            f"opslaglimiet overschreden: {used} > {storage_guard.max_bytes} bytes"
                        )
                        self._stop_process(process)
                        break
                    free = int(self._disk_usage(storage_guard.data_dir).free)
                    if free < storage_guard.min_free_bytes:
                        stopped_reason = (
                            f"vrije ruimte onder reserve: {free} < "
                            f"{storage_guard.min_free_bytes} bytes"
                        )
                        self._stop_process(process)
                        break
                self._sleep(self._poll_interval)

            returncode = process.wait(timeout=10)
            for thread in threads:
                thread.join(timeout=10)

        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        result = CommandResult(
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_log=stdout_path,
            stderr_log=stderr_path,
            route=self.detect_route(stderr, command),
            stopped_reason=stopped_reason,
        )
        self.last_result = result
        if stopped_reason:
            raise StorageLimitExceeded(stopped_reason)
        if check and returncode != 0:
            raise ArcticCommandError(result)
        return result

    def _stream_jsonl(
        self,
        command: tuple[str, ...],
        *,
        label: str,
        limit: int,
        timeout: int,
    ) -> Iterator[dict[str, Any]]:
        stdout_path, stderr_path = self._new_log_paths(label)
        process = self._start_process(command)
        stderr_parts: list[str] = []
        count = 0
        started = self._monotonic()
        stdout_text: list[str] = []

        try:
            with (
                stdout_path.open("w", encoding="utf-8", newline="") as stdout_log,
                stderr_path.open("w", encoding="utf-8", newline="") as stderr_log,
            ):
                stderr_thread = self._pump_thread(process.stderr, stderr_log, stderr_parts)
                if process.stdout is None:
                    raise ArcticOutputError("Arctic stdout-pipe ontbreekt.")
                for raw_line in process.stdout:
                    if self._monotonic() - started > timeout:
                        self._stop_process(process)
                        raise ArcticCommandError(
                            CommandResult(
                                command=command,
                                returncode=-1,
                                stdout="".join(stdout_text),
                                stderr="".join(stderr_parts),
                                stdout_log=stdout_path,
                                stderr_log=stderr_path,
                                stopped_reason=f"proces-timeout na {timeout} seconden",
                            )
                        )
                    stdout_log.write(raw_line)
                    stdout_log.flush()
                    stdout_text.append(raw_line)
                    line = raw_line.strip()
                    if not line:
                        continue
                    if limit and count >= limit:
                        self._stop_process(process)
                        raise ArcticOutputError("Arctic gaf meer records dan de ingestelde limiet.")
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        self._stop_process(process)
                        raise ArcticOutputError(f"Ongeldige Arctic JSONL-regel: {exc}") from exc
                    if not isinstance(record, dict):
                        self._stop_process(process)
                        raise ArcticOutputError("Een Arctic JSONL-record moet een object zijn.")
                    count += 1
                    yield record
                returncode = process.wait(timeout=max(1, timeout))
                stderr_thread.join(timeout=10)
        finally:
            if process.poll() is None:
                self._stop_process(process)

        result = CommandResult(
            command=command,
            returncode=returncode,
            stdout="".join(stdout_text),
            stderr="".join(stderr_parts),
            stdout_log=stdout_path,
            stderr_log=stderr_path,
        )
        self.last_result = result
        # Exit 3 is Arctic's documented no-data result and is valid for a query.
        if returncode not in (0, 3):
            raise ArcticCommandError(result)

    def _start_process(self, command: Sequence[str]) -> ProcessLike:
        try:
            return self._popen(
                list(command),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
        except FileNotFoundError as exc:
            raise ArcticError(
                "Arctic CLI is niet gevonden. Voer eerst `spell-collector doctor` uit."
            ) from exc

    def _new_log_paths(self, label: str) -> tuple[Path, Path]:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "-", label).strip("-") or "arctic"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        run_id = f"{stamp}-{uuid4().hex[:8]}-{safe_label}"
        return self.logs_dir / f"{run_id}.stdout.log", self.logs_dir / f"{run_id}.stderr.log"

    @staticmethod
    def _pump_thread(stream: TextIO | None, sink: TextIO, parts: list[str]) -> threading.Thread:
        def pump() -> None:
            if stream is None:
                return
            for chunk in stream:
                parts.append(chunk)
                sink.write(chunk)
                sink.flush()

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        return thread

    @staticmethod
    def _stop_process(process: ProcessLike) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _data_args(self) -> tuple[str, ...]:
        if self.data_dir is None:
            return ()
        return ("--data-dir", str(self.data_dir))

    def _require_data_dir(self) -> Path:
        if self.data_dir is None:
            raise AcquisitionGuardError("Een project-local data_dir is verplicht.")
        return self.data_dir

    @staticmethod
    def _coerce_args(args: Sequence[str] | str, more_args: Sequence[str]) -> tuple[str, ...]:
        if isinstance(args, str):
            return (args, *more_args)
        if more_args:
            raise TypeError("Gebruik een reeks of losse argumenten, niet beide.")
        return tuple(str(item) for item in args)

    @staticmethod
    def _normalize_subreddit(value: str) -> str:
        name = value.strip().strip("/")
        if name.casefold().startswith("r/"):
            name = name[2:]
        if not _SUBREDDIT_RE.fullmatch(name):
            raise AcquisitionGuardError(f"Ongeldige subredditnaam: {value!r}")
        return name

    @staticmethod
    def _require_date(value: str | date | None, label: str) -> date:
        if value is None:
            raise AcquisitionGuardError(f"Een expliciete {label}-datum is verplicht.")
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise AcquisitionGuardError(f"{label} moet YYYY-MM-DD zijn.") from exc

    @staticmethod
    def _directory_size(path: Path) -> int:
        total = 0
        if not path.exists():
            return 0
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                # A temp file can disappear between directory enumeration and
                # stat while Arctic atomically replaces it.
                continue
        return total

    @staticmethod
    def _parse_json_object(raw: str, label: str) -> dict[str, Any]:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ArcticOutputError(f"{label} gaf ongeldige JSON: {exc}") from exc
        if isinstance(value, list) and value:
            value = value[0]
        if not isinstance(value, dict):
            raise ArcticOutputError(f"{label} gaf geen JSON-object.")
        return value


__all__ = [
    "AcquisitionGuardError",
    "AcquisitionPlan",
    "ArcticCLI",
    "ArcticCommandError",
    "ArcticError",
    "ArcticOutputError",
    "CommandResult",
    "DoctorReport",
    "Route",
    "StorageLimitExceeded",
    "UnsafeArcticCommand",
]
