from __future__ import annotations

import sqlite3
from pathlib import Path

import spell_collector.config as config_module
from spell_collector.config import ConfigError, load_config
from spell_collector.db import Database, open_database
from spell_collector.models import (
    CompletenessStatus,
    RawRecord,
    SpellEvidence,
    SpellRecord,
)


def test_default_config_resolves_paths_from_project() -> None:
    config = load_config()
    assert config.storage.database == config.project_root / "data" / "spells.sqlite3"
    assert config.collection.pilot_max_records == 100
    assert "Spells" in config.collection.subreddits
    assert len(config.analysis.categories) >= 10


def test_override_config_is_merged_and_relative_to_its_project(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    override = config_dir / "local.yaml"
    override.write_text(
        "storage:\n  database: var/test.sqlite3\ncollection:\n  pilot_max_records: 25\n",
        encoding="utf-8",
    )
    config = load_config(override)
    assert config.project_root == tmp_path
    assert config.storage.database == tmp_path / "var" / "test.sqlite3"
    assert config.collection.pilot_max_records == 25
    assert config.collection.subreddits


def test_packaged_default_uses_current_directory_for_runtime_data(tmp_path, monkeypatch) -> None:
    packaged = Path(config_module.__file__).with_name("default.yaml")
    assert packaged.is_file()
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", packaged)
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.project_root == tmp_path
    assert config.storage.database == tmp_path / "data" / "spells.sqlite3"


def test_pilot_limit_cannot_exceed_100(tmp_path) -> None:
    config_path = tmp_path / "bad.yaml"
    config_path.write_text("collection:\n  pilot_max_records: 101\n", encoding="utf-8")
    try:
        load_config(config_path, project_root=tmp_path)
    except ConfigError as exc:
        assert "tussen 1 en 100" in str(exc)
    else:
        raise AssertionError("An unsafe pilot limit should be rejected")


def test_database_schema_wal_and_idempotent_upserts(tmp_path) -> None:
    database = Database(tmp_path / "spells.sqlite3")
    database.initialize()

    record = RawRecord(
        source_id="abc123",
        source_kind="post",
        subreddit="Spells",
        title="Candle rite",
        body="Light a candle, state the intention, then extinguish it safely.",
        is_candidate=True,
        processed_at="2026-11-05T10:00:00+00:00",
    )
    first_id = database.upsert_raw_record(record)
    record.body = "Light a white candle, state the intention, then extinguish it safely."
    second_id = database.upsert_raw_record(record)
    assert first_id == second_id

    spell = SpellRecord(
        unique_id="spell-001",
        title="Kaarsritueel voor intentie",
        title_original="Candle rite",
        language="en",
        category="Kaarsen, kruiden en kristallen",
        purpose="Een intentie symbolisch bekrachtigen.",
        materials=["white candle"],
        instructions=["Light a white candle.", "State the intention."],
        closing_steps=["Extinguish it safely."],
        source_subreddit="Spells",
        source_post_id="abc123",
        completeness_status=CompletenessStatus.COMPLETE,
        completeness_score=0.9,
        extraction_confidence=0.92,
    )
    database.upsert_spell(spell)
    spell.tags.append("candle")
    database.upsert_spell(spell)
    loaded = database.get_spell("spell-001")
    assert loaded is not None
    assert loaded.tags == ["candle"]
    assert loaded.materials == ["white candle"]

    evidence = SpellEvidence(
        spell_id="spell-001",
        field_name="instructions",
        raw_record_id=first_id,
        quote="Light a white candle",
        start_offset=0,
        end_offset=20,
    )
    assert database.add_evidence(evidence) == database.add_evidence(evidence)
    database.add_spell_source("spell-001", first_id)
    database.add_spell_source("spell-001", first_id)

    database.save_checkpoint(
        "analyze", "Spells", cursor={"offset": 1}, last_source_id="abc123", records_processed=1
    )
    checkpoint = database.get_checkpoint("analyze", "Spells")
    assert checkpoint is not None
    assert checkpoint["cursor"] == {"offset": 1}
    assert checkpoint["records_processed"] == 1

    stats = database.statistics()
    assert stats.posts == 1
    assert stats.potential_spells == 1
    assert stats.complete_spells == 1

    with sqlite3.connect(database.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM spells").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM spell_evidence").fetchone()[0] == 1


def test_open_database_context_manager_initializes_schema(tmp_path) -> None:
    path = tmp_path / "nested" / "collector.sqlite3"
    with open_database(path) as database:
        assert database.statistics().posts == 0
    assert path.exists()
