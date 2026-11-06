from __future__ import annotations

import io
import json
import subprocess
from collections import namedtuple
from pathlib import Path
from typing import Any

import pytest

from spell_collector.sources.arctic_cli import (
    AcquisitionGuardError,
    ArcticCLI,
    ArcticOutputError,
    Route,
    StorageLimitExceeded,
    UnsafeArcticCommand,
)

Disk = namedtuple("Disk", "total used free")


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        polls_before_exit: int = 0,
    ) -> None:
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self._final_returncode = returncode
        self._polls_before_exit = polls_before_exit
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if self._polls_before_exit > 0:
            self._polls_before_exit -= 1
            return None
        self.returncode = self._final_returncode
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = self._final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class PopenRecorder:
    def __init__(self, *processes: FakeProcess) -> None:
        self.processes = list(processes)
        self.commands: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> FakeProcess:
        self.commands.append(command)
        self.kwargs.append(kwargs)
        if not self.processes:
            raise AssertionError("Onverwachte extra subprocess-aanroep")
        return self.processes.pop(0)


def disk_with_free(free: int):
    def get_disk_usage(path: str | Path) -> Disk:
        del path
        return Disk(total=free * 2, used=free, free=free)

    return get_disk_usage


def make_cli(tmp_path: Path, recorder: PopenRecorder | None = None, *, free: int = 10_000):
    return ArcticCLI(
        data_dir=tmp_path / "data" / "arctic",
        logs_dir=tmp_path / "logs",
        popen_factory=recorder or PopenRecorder(),
        disk_usage=disk_with_free(free),
        which=lambda _: "C:/tools/arctic.exe",
        sleep=lambda _: None,
        poll_interval=0.01,
    )


@pytest.mark.parametrize(
    ("after", "before", "max_bytes", "min_free_bytes", "message"),
    [
        (None, "2026-11-02", 1000, 100, "after"),
        ("2026-11-01", None, 1000, 100, "before"),
        ("2026-11-01", "2026-11-02", None, 100, "max-bytes"),
        ("2026-11-01", "2026-11-02", 1000, None, "vrije-ruimtereserve"),
    ],
)
def test_plan_hard_refuses_missing_bounds(
    tmp_path: Path,
    after: str | None,
    before: str | None,
    max_bytes: int | None,
    min_free_bytes: int | None,
    message: str,
) -> None:
    cli = make_cli(tmp_path)

    with pytest.raises(AcquisitionGuardError, match=message):
        cli.plan_acquisition(
            "Spells",
            after=after,
            before=before,
            max_bytes=max_bytes,
            min_free_bytes=min_free_bytes,
        )


def test_plan_is_api_only_project_local_and_single_worker(tmp_path: Path) -> None:
    cli = make_cli(tmp_path, free=20_000)

    plan = cli.plan_acquisition(
        "r/Spells",
        after="2026-11-01",
        before="2026-11-05",
        max_bytes=5_000,
        min_free_bytes=1_000,
        timeout_seconds=45,
    )

    assert plan.subreddit == "Spells"
    assert plan.data_dir == (tmp_path / "data" / "arctic").resolve()
    assert plan.allowed_additional_bytes == 5_000
    assert "--api" in plan.command
    assert plan.command[plan.command.index("--workers") + 1] == "1"
    assert plan.command[plan.command.index("--timeout") + 1] == "45s"
    assert plan.command[plan.command.index("--data-dir") + 1] == str(plan.data_dir)
    assert "pull" not in plan.command
    assert "publish" not in plan.command


def test_plan_rejects_large_window_and_insufficient_disk(tmp_path: Path) -> None:
    cli = make_cli(tmp_path, free=2_000)

    with pytest.raises(AcquisitionGuardError, match="Datumvenster"):
        cli.plan_acquisition(
            "Spells",
            after="2026-01-01",
            before="2026-03-01",
            max_bytes=500,
            min_free_bytes=100,
        )

    with pytest.raises(AcquisitionGuardError, match="Onvoldoende vrije ruimte"):
        cli.plan_acquisition(
            "Spells",
            after="2026-01-01",
            before="2026-01-02",
            max_bytes=1_500,
            min_free_bytes=1_000,
        )


def test_acquire_logs_both_streams_and_detects_forced_api(tmp_path: Path) -> None:
    process = FakeProcess(stdout="done\n", stderr="Spells comments: 12 records\n")
    recorder = PopenRecorder(process)
    cli = make_cli(tmp_path, recorder, free=20_000)

    result = cli.acquire_subreddit(
        "Spells",
        after="2026-11-01",
        before="2026-11-02",
        max_bytes=5_000,
        min_free_bytes=1_000,
    )

    assert result.returncode == 0
    assert result.route is Route.API
    assert result.stdout_log.read_text(encoding="utf-8") == "done\n"
    assert result.stderr_log.read_text(encoding="utf-8") == "Spells comments: 12 records\n"
    assert recorder.kwargs[0]["shell"] is False
    assert "--api" in recorder.commands[0]


def test_active_storage_guard_terminates_process(tmp_path: Path) -> None:
    data_dir = tmp_path / "data" / "arctic"
    process = FakeProcess(polls_before_exit=100)

    def spawn(command: list[str], **kwargs: Any) -> FakeProcess:
        del command, kwargs
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "oversized.jsonl").write_bytes(b"x" * 501)
        return process

    cli = ArcticCLI(
        data_dir=data_dir,
        logs_dir=tmp_path / "logs",
        popen_factory=spawn,
        disk_usage=disk_with_free(10_000),
        sleep=lambda _: None,
        poll_interval=0.01,
    )

    with pytest.raises(StorageLimitExceeded, match="opslaglimiet"):
        cli.acquire_subreddit(
            "Spells",
            after="2026-11-01",
            before="2026-11-02",
            max_bytes=500,
            min_free_bytes=100,
        )

    assert process.terminated is True


@pytest.mark.parametrize("command", ["pull", "publish", "process", "sub", "user", "query"])
def test_generic_command_refuses_data_operations(tmp_path: Path, command: str) -> None:
    cli = make_cli(tmp_path)

    with pytest.raises(UnsafeArcticCommand):
        cli.command((command, "Spells"))


def test_help_is_read_only_even_for_forbidden_command(tmp_path: Path) -> None:
    recorder = PopenRecorder(FakeProcess(stdout="Usage: arctic pull\n"))
    cli = make_cli(tmp_path, recorder)

    result = cli.help("pull")

    assert result.returncode == 0
    assert recorder.commands[0][-2:] == ["pull", "--help"]


def test_doctor_reports_missing_binary_without_spawning(tmp_path: Path) -> None:
    recorder = PopenRecorder()
    cli = ArcticCLI(
        binary="missing-arctic",
        data_dir=tmp_path / "data",
        popen_factory=recorder,
        which=lambda _: None,
    )

    report = cli.doctor()

    assert report.ok is False
    assert report.resolved_binary is None
    assert "niet gevonden" in report.issues[0]
    assert recorder.commands == []


def test_doctor_reads_version_and_info_json(tmp_path: Path) -> None:
    recorder = PopenRecorder(
        FakeProcess(stdout='[{"version":"0.1.0","commit":"abc"}]\n'),
        FakeProcess(stdout='[{"os":"windows","disk_free_gb":100}]\n'),
    )
    cli = make_cli(tmp_path, recorder)

    report = cli.doctor()

    assert report.ok is True
    assert report.version == {"version": "0.1.0", "commit": "abc"}
    assert report.info == {"os": "windows", "disk_free_gb": 100}
    assert [command[-3] for command in recorder.commands] == ["version", "info"]


def test_query_streams_bounded_jsonl_and_logs(tmp_path: Path) -> None:
    rows = [{"id": "a", "body": "one"}, {"id": "b", "title": "two"}]
    stdout = "".join(json.dumps(row) + "\n" for row in rows)
    recorder = PopenRecorder(FakeProcess(stdout=stdout, stderr="query ok\n"))
    cli = make_cli(tmp_path, recorder)

    result = list(cli.query_jsonl("Spells", limit=2, kind="both", contains="ritual"))

    assert result == rows
    command = recorder.commands[0]
    assert command[command.index("-n") + 1] == "2"
    assert command[command.index("-o") + 1] == "jsonl"
    assert command[command.index("--workers") + 1] == "1"
    assert "--contains" in command
    assert cli.last_result is not None
    assert cli.last_result.stdout_log.read_text(encoding="utf-8") == stdout
    assert cli.last_result.stderr_log.read_text(encoding="utf-8") == "query ok\n"


def test_query_accepts_arctic_no_data_exit_code(tmp_path: Path) -> None:
    recorder = PopenRecorder(FakeProcess(returncode=3))
    cli = make_cli(tmp_path, recorder)

    assert list(cli.query_jsonl("Spells", limit=100)) == []
    assert cli.last_result is not None
    assert cli.last_result.returncode == 3


def test_query_rejects_zero_limit_and_bad_json(tmp_path: Path) -> None:
    cli = make_cli(tmp_path)
    with pytest.raises(AcquisitionGuardError, match="groter dan nul"):
        list(cli.query_jsonl("Spells", limit=0))

    recorder = PopenRecorder(FakeProcess(stdout="not-json\n"))
    cli = make_cli(tmp_path, recorder)
    with pytest.raises(ArcticOutputError, match="Ongeldige Arctic JSONL"):
        list(cli.query_jsonl("Spells", limit=1))


def test_route_detection_uses_flags_and_stderr() -> None:
    assert ArcticCLI.detect_route("", ("arctic", "sub", "Spells", "--api")) is Route.API
    assert ArcticCLI.detect_route("downloading from the torrent bundle") is Route.TORRENT
    assert ArcticCLI.detect_route("torrent catalog failed; using the API") is Route.API
    assert ArcticCLI.detect_route("nothing relevant") is Route.UNKNOWN


def test_stop_process_escalates_after_terminate_timeout() -> None:
    class StubbornProcess(FakeProcess):
        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            if not self.killed and timeout is not None:
                raise subprocess.TimeoutExpired("arctic", timeout)
            return super().wait(timeout)

    process = StubbornProcess(polls_before_exit=100)
    ArcticCLI._stop_process(process)

    assert process.terminated is True
    assert process.killed is True
