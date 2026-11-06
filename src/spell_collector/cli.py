"""Commandline-interface voor Magical Spell Collector."""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from spell_collector import __version__
from spell_collector.config import AppConfig, load_config
from spell_collector.db import Database
from spell_collector.exporters import export_all, verify_exports
from spell_collector.ollama import OllamaAnalyzer, OllamaUnavailable
from spell_collector.pipeline import (
    analyze_pending,
    check_storage_budget,
    collect_pilot,
    collect_subreddits,
)
from spell_collector.sources.arctic_cli import ArcticCLI, ArcticError

app = typer.Typer(
    name="spell-collector",
    help="Verzamel, analyseer en exporteer historische rituelen met herleidbare bronnen.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
arctic_app = typer.Typer(help="Veilige, begrensde Arctic CLI-functies.", no_args_is_help=True)
rights_app = typer.Typer(help="Lokale rechten- en verwijderingscontrole.", no_args_is_help=True)
app.add_typer(arctic_app, name="arctic")
app.add_typer(rights_app, name="rights")

console = Console()


@dataclass(slots=True)
class State:
    config: AppConfig
    json_output: bool


def _state(ctx: typer.Context) -> State:
    return ctx.find_root().obj


def _emit(state: State, value: Any) -> None:
    if state.json_output:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    else:
        console.print(value)


def _abort(state: State, exc: BaseException) -> None:
    if state.json_output:
        typer.echo(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}},
                ensure_ascii=False,
            )
        )
    else:
        console.print(f"[red]Fout:[/red] {exc}")
    raise typer.Exit(1)


def _database(config: AppConfig) -> Database:
    config.ensure_directories()
    database = Database(config.storage.database)
    database.initialize()
    return database


def _arctic_binary() -> str:
    configured = os.environ.get("ARCTIC_BIN")
    if configured:
        return configured
    found = shutil.which("arctic")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / ("arctic.exe" if os.name == "nt" else "arctic")
    return str(candidate) if candidate.is_file() else "arctic"


def _ram_bytes() -> int | None:
    if os.name == "nt":

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_physical)
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return None


def _system_info(config: AppConfig) -> dict[str, Any]:
    disk = shutil.disk_usage(config.project_root)
    return {
        "os": platform.platform(),
        "architecture": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "ram_bytes": _ram_bytes(),
        "disk": {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free},
        "python": sys.version,
    }


@app.callback()
def main(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None,
        "--config",
        help="Eigen YAML-configuratie; wordt over config/default.yaml gelegd.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Geef stabiele machineleesbare JSON op stdout.",
    ),
) -> None:
    """Gratis lokale verzamel- en analysetool; geen Reddit-bypass of betaalde API nodig."""

    ctx.obj = State(load_config(config), json_output)


@app.command()
def version(ctx: typer.Context) -> None:
    """Toon de applicatieversie."""

    state = _state(ctx)
    _emit(state, {"version": __version__} if state.json_output else __version__)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Controleer hardware, opslag, database, Arctic CLI en optioneel Ollama."""

    state = _state(ctx)
    try:
        database = _database(state.config)
        budget = check_storage_budget(state.config)
        arctic = ArcticCLI(
            binary=_arctic_binary(),
            data_dir=state.config.storage.raw_dir / "arctic",
            logs_dir=state.config.storage.log_dir / "arctic",
        ).doctor()
        ollama: dict[str, Any]
        try:
            models = OllamaAnalyzer(
                state.config.ollama.model or "unused",
                base_url=state.config.ollama.base_url,
                timeout=5,
            ).models()
            ollama = {"available": True, "models": models}
        except OllamaUnavailable as exc:
            ollama = {"available": False, "models": [], "issue": str(exc)}
        result = {
            "ok": arctic.ok,
            "system": _system_info(state.config),
            "storage": budget,
            "database": {
                "path": str(state.config.storage.database),
                "writable": True,
                "stats": database.statistics().as_dict(),
            },
            "arctic": arctic.as_dict(),
            "ollama": ollama,
            "offline_mode": True,
            "auth_required": False,
        }
        if state.json_output:
            _emit(state, result)
        else:
            table = Table(title="Magical Spell Collector — doctor")
            table.add_column("Onderdeel")
            table.add_column("Status")
            table.add_row("Python", platform.python_version())
            table.add_row("CPU", f"{os.cpu_count()} logische cores")
            table.add_row("RAM", f"{(_ram_bytes() or 0) / 1024**3:.1f} GB")
            table.add_row("Vrije schijf", f"{budget['free_bytes'] / 1024**3:.1f} GB")
            table.add_row("Arctic CLI", "OK" if arctic.ok else "; ".join(arctic.issues))
            table.add_row(
                "Ollama",
                f"{len(ollama['models'])} lokaal model(len)"
                if ollama["available"]
                else "niet bereikbaar",
            )
            table.add_row("SQLite", str(state.config.storage.database))
            console.print(table)
    except Exception as exc:
        _abort(state, exc)


@app.command()
def pilot(
    ctx: typer.Context,
    limit: int = typer.Option(100, min=1, max=100, help="Harde totaalgrens voor bronrecords."),
    source: str = typer.Option("auto", help="auto, arctic, api of fixture."),
    after: str | None = typer.Option(None, help="Begindatum YYYY-MM-DD."),
    before: str | None = typer.Option(None, help="Einddatum YYYY-MM-DD (exclusief)."),
    fixture: Path | None = typer.Option(
        None, help="Offline JSONL-fixture voor reproduceerbare test."
    ),
    use_ai: bool = typer.Option(
        False, "--use-ai", help="Gebruik het geconfigureerde lokale Ollama-model."
    ),
    model: str | None = typer.Option(None, help="Ollama-modelnaam; overschrijft de config."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Toon alleen grenzen en voer niets uit."),
) -> None:
    """Voer een end-to-end proef uit op maximaal 100 r/Spells-records."""

    state = _state(ctx)
    try:
        budget = check_storage_budget(state.config, expected_bytes=16 * 1024 * 1024)
        preview = {
            "subreddit": state.config.collection.pilot_subreddit,
            "hard_record_limit": limit,
            "max_single_api_response_bytes": 16 * 1024 * 1024,
            "project_storage_limit_bytes": budget["max_bytes"],
            "free_bytes": budget["free_bytes"],
            "source": "fixture" if fixture else source,
            "note": "Arctic CLI-grootte is vooraf onbekend; opslag wordt tijdens de taak hard bewaakt.",
        }
        if dry_run:
            _emit(state, {"ok": True, "plan": preview})
            return
        if not state.json_output:
            console.print(
                f"Pilot: maximaal [bold]{limit}[/bold] records; projectlimiet "
                f"{budget['max_bytes'] / 1024**3:.1f} GB; vrije ruimte "
                f"{budget['free_bytes'] / 1024**3:.1f} GB."
            )
        database = _database(state.config)
        collected = collect_pilot(
            database,
            state.config,
            max_records=limit,
            source=source,
            after=after,
            before=before,
            fixture=fixture,
            arctic_binary=_arctic_binary(),
        )
        analyzed = analyze_pending(
            database,
            state.config,
            limit=0,
            use_ai=use_ai,
            model=model,
        )
        exported = export_all(database, state.config)
        _emit(
            state,
            {
                "ok": True,
                "plan": preview,
                "collection": collected.as_dict(),
                "analysis": analyzed.as_dict(),
                "export": exported.as_dict(),
            },
        )
    except Exception as exc:
        _abort(state, exc)


@app.command()
def collect(
    ctx: typer.Context,
    after: str = typer.Option(..., help="Begindatum YYYY-MM-DD."),
    before: str = typer.Option(..., help="Einddatum YYYY-MM-DD (exclusief)."),
    subreddit: list[str] = typer.Option(
        [], "--subreddit", "-s", help="Herhaalbaar; standaard configlijst."
    ),
    max_records: int = typer.Option(
        1000,
        min=0,
        help="Per subreddit; 0 streamt alle lokaal geïmporteerde records.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Toon planning zonder netwerktaak."),
) -> None:
    """Verzamel via Arctic CLI in veilige vensters van maximaal 31 dagen."""

    state = _state(ctx)
    communities = subreddit or list(state.config.collection.subreddits)
    try:
        budget = check_storage_budget(state.config)
        plan = {
            "subreddits": communities,
            "after": after,
            "before": before,
            "max_records_per_subreddit": max_records,
            "project_storage_limit_bytes": budget["max_bytes"],
            "reserve_free_bytes": budget["reserve_bytes"],
            "route": "Arctic CLI --api, vensters van maximaal 31 dagen",
        }
        if dry_run:
            _emit(state, {"ok": True, "plan": plan})
            return
        database = _database(state.config)
        outcomes = collect_subreddits(
            database,
            state.config,
            subreddits=communities,
            after=after,
            before=before,
            max_records_per_subreddit=max_records,
            arctic_binary=_arctic_binary(),
        )
        _emit(
            state, {"ok": True, "plan": plan, "collections": [item.as_dict() for item in outcomes]}
        )
    except Exception as exc:
        _abort(state, exc)


@app.command()
def analyze(
    ctx: typer.Context,
    limit: int = typer.Option(
        0, min=0, help="0 analyseert alle nog niet verwerkte lokale records."
    ),
    batch_size: int | None = typer.Option(None, min=1, help="Checkpointgrootte."),
    use_ai: bool = typer.Option(False, "--use-ai", help="Gebruik lokaal Ollama."),
    model: str | None = typer.Option(None, help="Lokaal Ollama-model."),
    reanalyze: str = typer.Option(
        "none",
        help="none, review, incomplete of all; zet geselecteerde bronnen opnieuw klaar.",
    ),
    rebuild: bool = typer.Option(
        False,
        "--rebuild",
        help="Bouw alle afgeleide analyses opnieuw; ruwe bronrecords blijven behouden.",
    ),
) -> None:
    """Analyseer lokaal en hervat automatisch vanaf checkpoints."""

    state = _state(ctx)
    try:
        database = _database(state.config)
        requeued = 0
        if rebuild:
            database.reset_analysis()
        elif reanalyze != "none":
            status_map = {
                "review": ("REVIEW",),
                "incomplete": ("INCOMPLETE",),
                "all": ("COMPLETE", "INCOMPLETE", "REVIEW", "DUPLICATE"),
            }
            if reanalyze not in status_map:
                raise ValueError("--reanalyze moet none, review, incomplete of all zijn")
            requeued = database.requeue_spells_for_analysis(status_map[reanalyze])
        outcome = analyze_pending(
            database,
            state.config,
            limit=limit,
            batch_size=batch_size,
            use_ai=use_ai,
            model=model,
        )
        _emit(
            state,
            {
                "ok": True,
                "analysis": outcome.as_dict(),
                "requeued": requeued,
                "rebuilt": rebuild,
            },
        )
    except Exception as exc:
        _abort(state, exc)


@app.command()
def resume(
    ctx: typer.Context,
    use_ai: bool = typer.Option(
        False, "--use-ai", help="Gebruik lokaal Ollama bij resterende analyse."
    ),
    model: str | None = typer.Option(None, help="Lokaal Ollama-model."),
) -> None:
    """Hervat niet-verwerkte lokale records zonder dubbele ingestie."""

    state = _state(ctx)
    try:
        database = _database(state.config)
        pending_jobs = database.list_resumable_jobs()
        requeued = database.requeue_retryable_analysis_errors()
        outcome = analyze_pending(database, state.config, limit=0, use_ai=use_ai, model=model)
        _emit(
            state,
            {
                "ok": True,
                "analysis": outcome.as_dict(),
                "requeued_analysis_errors": requeued,
                "previous_resumable_jobs": pending_jobs,
                "note": "Arctic-imports zijn idempotent; mislukte verzameltaken kunnen met hetzelfde collect-commando opnieuw worden gestart.",
            },
        )
    except Exception as exc:
        _abort(state, exc)


@app.command(name="export")
def export_command(ctx: typer.Context) -> None:
    """Maak JSON, JSONL, Excel-CSV, Markdown en het collectierapport."""

    state = _state(ctx)
    try:
        outcome = export_all(_database(state.config), state.config)
        _emit(state, {"ok": True, "export": outcome.as_dict()})
    except Exception as exc:
        _abort(state, exc)


@app.command()
def verify(ctx: typer.Context) -> None:
    """Controleer database/exportaantallen, formaten, veiligheid en bronvelden."""

    state = _state(ctx)
    try:
        result = verify_exports(_database(state.config), state.config)
        _emit(state, result)
        if not result["ok"]:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as exc:
        _abort(state, exc)


@app.command()
def stats(ctx: typer.Context) -> None:
    """Toon het terminaldashboard en de werkelijke database-aantallen."""

    state = _state(ctx)
    try:
        database = _database(state.config)
        payload = {
            "stats": database.statistics().as_dict(),
            "categories": database.category_counts(),
            "progress": database.progress(),
            "storage": check_storage_budget(state.config),
            "unresolved_errors": len(database.unresolved_errors()),
            "current_phase": "idle",
        }
        if state.json_output:
            _emit(state, payload)
            return
        table = Table(title="Magical Spell Collector — voortgang")
        table.add_column("Meting")
        table.add_column("Aantal", justify="right")
        labels = {
            "posts": "Berichten verzameld",
            "comments": "Reacties verzameld",
            "analyzed_records": "Records geanalyseerd",
            "potential_spells": "Potentiële spreuken",
            "complete_spells": "Complete spreuken",
            "incomplete_spells": "Onvolledige spreuken",
            "duplicate_spells": "Duplicaten",
            "review_spells": "Handmatige beoordeling",
            "safety_flagged": "Veiligheidsvlaggen",
        }
        for key, value in payload["stats"].items():
            table.add_row(labels.get(key, key), str(value))
        table.add_row("Onopgeloste fouten", str(payload["unresolved_errors"]))
        table.add_row("Gebruikte data", f"{payload['storage']['used_bytes'] / 1024**2:.1f} MB")
        console.print(table)
        if payload["categories"]:
            category_table = Table(title="Complete spreuken per categorie")
            category_table.add_column("Categorie")
            category_table.add_column("Aantal", justify="right")
            for name, count in payload["categories"].items():
                category_table.add_row(name, str(count))
            console.print(category_table)
    except Exception as exc:
        _abort(state, exc)


@arctic_app.command("info")
def arctic_info(ctx: typer.Context) -> None:
    """Voer `arctic info` veilig uit voor hardware- en padcontrole."""

    state = _state(ctx)
    try:
        wrapper = ArcticCLI(
            binary=_arctic_binary(),
            data_dir=state.config.storage.raw_dir / "arctic",
            logs_dir=state.config.storage.log_dir / "arctic",
        )
        _emit(state, {"ok": True, "arctic": wrapper.info()})
    except Exception as exc:
        _abort(state, exc)


@arctic_app.command("help")
def arctic_help(ctx: typer.Context, command: str | None = typer.Argument(None)) -> None:
    """Toon de echte Arctic-help zonder een datataak te starten."""

    state = _state(ctx)
    try:
        result = ArcticCLI(binary=_arctic_binary()).help(command)
        _emit(state, {"ok": True, "help": result.stdout} if state.json_output else result.stdout)
    except ArcticError as exc:
        _abort(state, exc)


@arctic_app.command("query")
def arctic_query(
    ctx: typer.Context,
    subreddit: str,
    limit: int = typer.Option(20, min=1, max=1000),
    contains: str | None = typer.Option(None),
) -> None:
    """Lees begrensd uit reeds lokaal geïmporteerde Arctic-data."""

    state = _state(ctx)
    try:
        records = list(
            ArcticCLI(
                binary=_arctic_binary(),
                data_dir=state.config.storage.raw_dir / "arctic",
                logs_dir=state.config.storage.log_dir / "arctic",
            ).query_jsonl(subreddit, limit=limit, contains=contains)
        )
        _emit(state, {"ok": True, "count": len(records), "records": records})
    except Exception as exc:
        _abort(state, exc)


@rights_app.command("approve")
def rights_approve(
    ctx: typer.Context,
    spell_id: str,
    status: str = typer.Option("PERMISSION_GRANTED", help="PERMISSION_GRANTED of PUBLIC_DOMAIN."),
    scope: str = typer.Option("structured_record"),
    rights_holder_reference: str = typer.Option(""),
    evidence_reference: str = typer.Option(""),
) -> None:
    """Registreer handmatig gecontroleerde publicatietoestemming."""

    state = _state(ctx)
    try:
        database = _database(state.config)
        database.approve_publication(
            spell_id,
            permission_scope=scope,
            rights_status=status,
            rights_holder_reference=rights_holder_reference,
            evidence_reference=evidence_reference,
        )
        _emit(state, {"ok": True, "spell_id": spell_id, "rights_status": status})
    except Exception as exc:
        _abort(state, exc)


@rights_app.command("remove")
def rights_remove(
    ctx: typer.Context,
    source_id: str,
    spell_id: str | None = typer.Option(None),
    requester_reference: str = typer.Option(""),
    reason: str = typer.Option(""),
) -> None:
    """Blokkeer een bron/spell lokaal wegens een verwijderingsverzoek."""

    state = _state(ctx)
    try:
        request_id = _database(state.config).add_removal_request(
            source_id=source_id,
            spell_id=spell_id,
            requester_reference=requester_reference,
            reason=reason,
        )
        _emit(state, {"ok": True, "removal_request_id": request_id})
    except Exception as exc:
        _abort(state, exc)
