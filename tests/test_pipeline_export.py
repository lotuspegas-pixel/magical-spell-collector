import csv
import json
from pathlib import Path

from spell_collector.config import load_config
from spell_collector.db import Database
from spell_collector.exporters import export_all, verify_exports
from spell_collector.pipeline import analyze_pending, collect_pilot

FIXTURE = Path(__file__).parent / "fixtures" / "pilot_records.jsonl"


def test_offline_pilot_is_bounded_resumable_and_exports(tmp_path: Path) -> None:
    config = load_config(project_root=tmp_path)
    database = Database(config.storage.database)
    database.initialize()

    collection = collect_pilot(
        database,
        config,
        max_records=6,
        source="fixture",
        fixture=FIXTURE,
    )
    assert collection.records_received == 6
    assert collection.records_received <= 100

    first = analyze_pending(database, config, batch_size=2)
    assert first.analyzed_records == 6
    assert first.spells_written >= 4

    second = analyze_pending(database, config, batch_size=2)
    assert second.analyzed_records == 0

    outcome = export_all(database, config)
    assert outcome.complete_records >= 1
    assert (config.storage.export_dir / "spells.json").is_file()
    assert (config.storage.export_dir / "spells.csv").is_file()
    assert (config.storage.export_dir / "complete-spell-collection.md").is_file()
    assert (config.storage.export_dir / "collection-report.md").is_file()

    records = json.loads((config.storage.export_dir / "spells.json").read_text(encoding="utf-8"))
    assert all(item["completeness_status"] == "COMPLETE" for item in records)
    assert all(item["safety_status"] == "SAFE" for item in records)
    assert not any(item["source_post_id"] == "fixture004" for item in records)

    with (config.storage.export_dir / "spells.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        csv_records = list(csv.DictReader(handle))
    assert len(csv_records) == len(records)
    verification = verify_exports(database, config)
    assert verification["ok"], verification["issues"]


def test_reingesting_fixture_is_idempotent(tmp_path: Path) -> None:
    config = load_config(project_root=tmp_path)
    database = Database(config.storage.database)
    database.initialize()
    for _ in range(2):
        collect_pilot(
            database,
            config,
            max_records=6,
            source="fixture",
            fixture=FIXTURE,
        )
    stats = database.statistics()
    assert stats.posts == 5
    assert stats.comments == 1


def test_reanalysis_never_links_a_spell_as_its_own_duplicate(tmp_path: Path) -> None:
    config = load_config(project_root=tmp_path)
    database = Database(config.storage.database)
    database.initialize()
    collect_pilot(
        database,
        config,
        max_records=1,
        source="fixture",
        fixture=FIXTURE,
    )
    first = analyze_pending(database, config)
    assert first.errors == 0
    with database.connect() as connection:
        connection.execute("UPDATE raw_records SET processed_at='' WHERE source_id='fixture001'")
    second = analyze_pending(database, config)
    assert second.errors == 0
    with database.connect() as connection:
        self_links = connection.execute(
            "SELECT COUNT(*) FROM duplicate_links WHERE master_spell_id=duplicate_spell_id"
        ).fetchone()[0]
    assert self_links == 0


def test_reset_analysis_preserves_raw_records(tmp_path: Path) -> None:
    config = load_config(project_root=tmp_path)
    database = Database(config.storage.database)
    database.initialize()
    collect_pilot(
        database,
        config,
        max_records=2,
        source="fixture",
        fixture=FIXTURE,
    )
    analyze_pending(database, config)
    database.reset_analysis()
    stats = database.statistics()
    assert stats.posts == 1
    assert stats.comments == 1
    assert stats.analyzed_records == 0
    rebuilt = analyze_pending(database, config)
    assert rebuilt.analyzed_records == 2
