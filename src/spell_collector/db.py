"""SQLite persistence with idempotent writes and resumable checkpoints."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from spell_collector.models import (
    DatabaseStats,
    RawRecord,
    SpellEvidence,
    SpellRecord,
    utc_now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_records (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT 'reddit',
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    subreddit TEXT NOT NULL,
    parent_id TEXT NOT NULL DEFAULT '',
    link_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    created_utc TEXT NOT NULL DEFAULT '',
    permalink TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    thread_key TEXT NOT NULL DEFAULT '',
    ingestion_job_id TEXT NOT NULL DEFAULT '',
    is_candidate INTEGER NOT NULL DEFAULT 0,
    processed_at TEXT NOT NULL DEFAULT '',
    date_collected TEXT NOT NULL,
    UNIQUE(platform, source_kind, source_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_subreddit_kind ON raw_records(subreddit, source_kind);
CREATE INDEX IF NOT EXISTS idx_raw_content_hash ON raw_records(content_hash);
CREATE INDEX IF NOT EXISTS idx_raw_processing ON raw_records(is_candidate, processed_at);

CREATE TABLE IF NOT EXISTS spells (
    unique_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    title_original TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    categories_json TEXT NOT NULL DEFAULT '[]',
    subcategories_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    purpose TEXT NOT NULL DEFAULT '',
    materials_json TEXT NOT NULL DEFAULT '[]',
    preparation_json TEXT NOT NULL DEFAULT '[]',
    instructions_json TEXT NOT NULL DEFAULT '[]',
    incantation TEXT NOT NULL DEFAULT '',
    timing TEXT NOT NULL DEFAULT '',
    closing_steps_json TEXT NOT NULL DEFAULT '[]',
    historical_tradition TEXT NOT NULL DEFAULT '',
    symbolism TEXT NOT NULL DEFAULT '',
    source_subreddit TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_post_id TEXT NOT NULL DEFAULT '',
    source_comment_ids_json TEXT NOT NULL DEFAULT '[]',
    source_date TEXT NOT NULL DEFAULT '',
    source_author TEXT NOT NULL DEFAULT '',
    original_text_reference TEXT NOT NULL DEFAULT '',
    structured_interpretation_nl TEXT NOT NULL DEFAULT '',
    completeness_status TEXT NOT NULL,
    completeness_score REAL NOT NULL DEFAULT 0,
    extraction_confidence REAL NOT NULL DEFAULT 0,
    safety_notes_json TEXT NOT NULL DEFAULT '[]',
    rights_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    review_status TEXT NOT NULL DEFAULT 'PENDING',
    date_collected TEXT NOT NULL,
    safety_status TEXT NOT NULL DEFAULT 'UNCHECKED',
    publication_status TEXT NOT NULL DEFAULT 'NOT_READY',
    rights_notes TEXT NOT NULL DEFAULT '',
    analysis_method TEXT NOT NULL DEFAULT 'heuristic',
    analysis_version TEXT NOT NULL DEFAULT '1',
    normalized_hash TEXT NOT NULL DEFAULT '',
    simhash TEXT NOT NULL DEFAULT '',
    removal_requested INTEGER NOT NULL DEFAULT 0,
    publication_permission INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spell_status ON spells(completeness_status, review_status);
CREATE INDEX IF NOT EXISTS idx_spell_category ON spells(category);
CREATE INDEX IF NOT EXISTS idx_spell_normalized_hash ON spells(normalized_hash);
CREATE INDEX IF NOT EXISTS idx_spell_publication
ON spells(publication_status, safety_status, rights_status);

CREATE TABLE IF NOT EXISTS spell_evidence (
    id INTEGER PRIMARY KEY,
    spell_id TEXT NOT NULL REFERENCES spells(unique_id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    raw_record_id INTEGER NOT NULL REFERENCES raw_records(id) ON DELETE CASCADE,
    quote TEXT NOT NULL,
    start_offset INTEGER,
    end_offset INTEGER,
    evidence_type TEXT NOT NULL DEFAULT 'source',
    created_at TEXT NOT NULL,
    UNIQUE(spell_id, field_name, raw_record_id, quote)
);

CREATE TABLE IF NOT EXISTS spell_sources (
    id INTEGER PRIMARY KEY,
    spell_id TEXT NOT NULL REFERENCES spells(unique_id) ON DELETE CASCADE,
    raw_record_id INTEGER NOT NULL REFERENCES raw_records(id) ON DELETE CASCADE,
    source_role TEXT NOT NULL DEFAULT 'primary',
    created_at TEXT NOT NULL,
    UNIQUE(spell_id, raw_record_id, source_role)
);

CREATE TABLE IF NOT EXISTS duplicate_links (
    id INTEGER PRIMARY KEY,
    master_spell_id TEXT NOT NULL REFERENCES spells(unique_id) ON DELETE CASCADE,
    duplicate_spell_id TEXT NOT NULL REFERENCES spells(unique_id) ON DELETE CASCADE,
    method TEXT NOT NULL,
    similarity REAL NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(master_spell_id, duplicate_spell_id, method),
    CHECK(master_spell_id <> duplicate_spell_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    status TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    cursor_json TEXT NOT NULL DEFAULT '{}',
    records_processed INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS checkpoints (
    stage TEXT NOT NULL,
    subreddit TEXT NOT NULL DEFAULT '',
    cursor_json TEXT NOT NULL DEFAULT '{}',
    last_source_id TEXT NOT NULL DEFAULT '',
    records_processed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(stage, subreddit)
);

CREATE TABLE IF NOT EXISTS subreddit_progress (
    subreddit TEXT NOT NULL,
    stage TEXT NOT NULL,
    posts_processed INTEGER NOT NULL DEFAULT 0,
    comments_processed INTEGER NOT NULL DEFAULT 0,
    candidates_found INTEGER NOT NULL DEFAULT 0,
    complete_found INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDING',
    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(subreddit, stage)
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL,
    subreddit TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    error_type TEXT NOT NULL,
    message TEXT NOT NULL,
    retryable INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_errors_unresolved ON errors(resolved_at, stage);

CREATE TABLE IF NOT EXISTS removal_requests (
    id INTEGER PRIMARY KEY,
    spell_id TEXT REFERENCES spells(unique_id) ON DELETE SET NULL,
    source_platform TEXT NOT NULL DEFAULT 'reddit',
    source_id TEXT NOT NULL DEFAULT '',
    requester_reference TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    requested_at TEXT NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT '',
    resolution_notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS publication_permissions (
    id INTEGER PRIMARY KEY,
    spell_id TEXT NOT NULL REFERENCES spells(unique_id) ON DELETE CASCADE,
    permission_scope TEXT NOT NULL,
    rights_holder_reference TEXT NOT NULL DEFAULT '',
    evidence_reference TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    granted_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(spell_id, permission_scope)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY,
    method TEXT NOT NULL,
    version TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    records_processed INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
"""


def _value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def upsert_raw_record(self, record: RawRecord) -> int:
        raw_json = record.raw_json if isinstance(record.raw_json, str) else _json(record.raw_json)
        content_hash = (
            record.content_hash or hashlib.sha256(record.text.encode("utf-8")).hexdigest()
        )
        values = (
            record.platform,
            _value(record.source_kind),
            record.source_id,
            record.subreddit,
            record.parent_id,
            record.link_id,
            record.title,
            record.body,
            record.author,
            record.created_utc,
            record.permalink,
            record.source_url,
            raw_json,
            content_hash,
            record.thread_key,
            record.ingestion_job_id,
            int(record.is_candidate),
            record.processed_at,
            record.date_collected,
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO raw_records (
                    platform, source_kind, source_id, subreddit, parent_id, link_id,
                    title, body, author, created_utc, permalink, source_url, raw_json,
                    content_hash, thread_key, ingestion_job_id, is_candidate,
                    processed_at, date_collected
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, source_kind, source_id) DO UPDATE SET
                    subreddit=excluded.subreddit, parent_id=excluded.parent_id,
                    link_id=excluded.link_id, title=excluded.title, body=excluded.body,
                    author=excluded.author, created_utc=excluded.created_utc,
                    permalink=excluded.permalink, source_url=excluded.source_url,
                    raw_json=excluded.raw_json, content_hash=excluded.content_hash,
                    thread_key=excluded.thread_key,
                    ingestion_job_id=excluded.ingestion_job_id,
                    is_candidate=MAX(raw_records.is_candidate, excluded.is_candidate),
                    processed_at=CASE WHEN excluded.processed_at <> ''
                        THEN excluded.processed_at ELSE raw_records.processed_at END
                """,
                values,
            )
            row = connection.execute(
                "SELECT id FROM raw_records WHERE platform=? AND source_kind=? AND source_id=?",
                (record.platform, _value(record.source_kind), record.source_id),
            ).fetchone()
            return int(row["id"])

    def upsert_spell(self, spell: SpellRecord) -> str:
        data = asdict(spell)
        json_fields = {
            "categories": "categories_json",
            "subcategories": "subcategories_json",
            "tags": "tags_json",
            "materials": "materials_json",
            "preparation": "preparation_json",
            "instructions": "instructions_json",
            "closing_steps": "closing_steps_json",
            "source_comment_ids": "source_comment_ids_json",
            "safety_notes": "safety_notes_json",
        }
        columns: list[str] = []
        values: list[Any] = []
        for name, value in data.items():
            columns.append(json_fields.get(name, name))
            if name in json_fields:
                values.append(_json(value))
            elif isinstance(value, bool):
                values.append(int(value))
            else:
                values.append(_value(value))
        columns.append("updated_at")
        values.append(utc_now())
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(
            f"{column}=excluded.{column}" for column in columns if column != "unique_id"
        )
        with self.connect() as connection:
            connection.execute(
                f"INSERT INTO spells ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(unique_id) DO UPDATE SET {updates}",
                values,
            )
        return spell.unique_id

    def get_spell(self, unique_id: str) -> SpellRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM spells WHERE unique_id=?", (unique_id,)
            ).fetchone()
        if row is None:
            return None
        mapping = dict(row)
        for field_name in (
            "categories",
            "subcategories",
            "tags",
            "materials",
            "preparation",
            "instructions",
            "closing_steps",
            "source_comment_ids",
            "safety_notes",
        ):
            mapping[field_name] = json.loads(mapping.pop(f"{field_name}_json"))
        mapping["removal_requested"] = bool(mapping["removal_requested"])
        mapping["publication_permission"] = bool(mapping["publication_permission"])
        mapping.pop("updated_at", None)
        return SpellRecord(**mapping)

    @staticmethod
    def _decode_spell_row(row: sqlite3.Row) -> dict[str, Any]:
        mapping = dict(row)
        for field_name in (
            "categories",
            "subcategories",
            "tags",
            "materials",
            "preparation",
            "instructions",
            "closing_steps",
            "source_comment_ids",
            "safety_notes",
        ):
            mapping[field_name] = json.loads(mapping.pop(f"{field_name}_json"))
        mapping["removal_requested"] = bool(mapping["removal_requested"])
        mapping["publication_permission"] = bool(mapping["publication_permission"])
        return mapping

    def fetch_unprocessed_raw(
        self,
        *,
        limit: int = 100,
        subreddit: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        query = "SELECT * FROM raw_records WHERE processed_at=''"
        values: list[Any] = []
        if subreddit:
            query += " AND subreddit=?"
            values.append(subreddit)
        query += " ORDER BY id LIMIT ?"
        values.append(limit)
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, values).fetchall()]

    def get_raw_record(self, raw_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM raw_records WHERE id=?", (raw_id,)).fetchone()
        return dict(row) if row else None

    def find_raw_record(self, source_kind: str, source_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM raw_records
                WHERE platform='reddit' AND source_kind=? AND source_id=?""",
                (source_kind, source_id.removeprefix("t1_").removeprefix("t3_")),
            ).fetchone()
        return dict(row) if row else None

    def linked_comments(self, post_id: str) -> list[dict[str, Any]]:
        bare = post_id.removeprefix("t3_")
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM raw_records
                WHERE source_kind='comment' AND (link_id=? OR link_id=?)
                ORDER BY created_utc, id""",
                (bare, f"t3_{bare}"),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_raw_processed(self, raw_id: int, *, is_candidate: bool) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE raw_records SET processed_at=?, is_candidate=? WHERE id=?",
                (utc_now(), int(is_candidate), raw_id),
            )

    def list_spells(
        self,
        *,
        completeness_status: str | None = None,
        include_duplicates: bool = True,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if completeness_status:
            clauses.append("completeness_status=?")
            values.append(completeness_status)
        if not include_duplicates:
            clauses.append("completeness_status<>'DUPLICATE'")
        query = "SELECT * FROM spells"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY category, title, unique_id"
        with self.connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [self._decode_spell_row(row) for row in rows]

    def find_spell_by_hash(self, normalized_hash: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM spells WHERE normalized_hash=? ORDER BY completeness_score DESC LIMIT 1",
                (normalized_hash,),
            ).fetchone()
        return self._decode_spell_row(row) if row else None

    def set_spell_completeness_status(self, unique_id: str, status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE spells SET completeness_status=?, updated_at=? WHERE unique_id=?",
                (status, utc_now(), unique_id),
            )

    def create_job(
        self,
        command: str,
        *,
        config: dict[str, Any] | None = None,
        status: str = "PENDING",
        job_id: str | None = None,
    ) -> str:
        identifier = job_id or uuid.uuid4().hex
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO jobs
                (job_id, command, status, config_json, cursor_json, started_at, updated_at)
                VALUES (?, ?, ?, ?, '{}', ?, ?)""",
                (identifier, command, status, _json(config or {}), now, now),
            )
        return identifier

    def update_job(
        self,
        job_id: str,
        *,
        status: str,
        records_processed: int | None = None,
        cursor: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> None:
        assignments = ["status=?", "updated_at=?", "error_message=?"]
        values: list[Any] = [status, utc_now(), error_message]
        if records_processed is not None:
            assignments.append("records_processed=?")
            values.append(records_processed)
        if cursor is not None:
            assignments.append("cursor_json=?")
            values.append(_json(cursor))
        if status == "COMPLETE":
            assignments.append("completed_at=?")
            values.append(utc_now())
        values.append(job_id)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id=?",
                values,
            )

    def list_resumable_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status IN ('PENDING','PAUSED','FAILED') ORDER BY updated_at"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["config"] = json.loads(item.pop("config_json"))
            item["cursor"] = json.loads(item.pop("cursor_json"))
            result.append(item)
        return result

    def update_progress(
        self,
        subreddit: str,
        stage: str,
        *,
        posts: int = 0,
        comments: int = 0,
        candidates: int = 0,
        complete: int = 0,
        bytes_downloaded: int = 0,
        status: str = "RUNNING",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO subreddit_progress
                (subreddit, stage, posts_processed, comments_processed, candidates_found,
                 complete_found, status, bytes_downloaded, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subreddit, stage) DO UPDATE SET
                  posts_processed=subreddit_progress.posts_processed+excluded.posts_processed,
                  comments_processed=subreddit_progress.comments_processed+excluded.comments_processed,
                  candidates_found=subreddit_progress.candidates_found+excluded.candidates_found,
                  complete_found=subreddit_progress.complete_found+excluded.complete_found,
                  status=excluded.status,
                  bytes_downloaded=subreddit_progress.bytes_downloaded+excluded.bytes_downloaded,
                  updated_at=excluded.updated_at""",
                (
                    subreddit,
                    stage,
                    posts,
                    comments,
                    candidates,
                    complete,
                    status,
                    bytes_downloaded,
                    utc_now(),
                ),
            )

    def progress(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM subreddit_progress ORDER BY subreddit, stage"
                ).fetchall()
            ]

    def rebuild_analysis_progress(self, *, status: str = "COMPLETE") -> None:
        """Herbereken voortgang uit bronrecords, zodat hervatten nooit dubbeltelt."""

        with self.connect() as connection:
            subreddits = connection.execute(
                "SELECT DISTINCT subreddit FROM raw_records ORDER BY subreddit"
            ).fetchall()
            for subreddit_row in subreddits:
                subreddit = str(subreddit_row["subreddit"])
                counts = connection.execute(
                    """SELECT
                      SUM(CASE WHEN source_kind='post' AND processed_at<>'' THEN 1 ELSE 0 END) posts,
                      SUM(CASE WHEN source_kind='comment' AND processed_at<>'' THEN 1 ELSE 0 END) comments,
                      SUM(CASE WHEN is_candidate=1 THEN 1 ELSE 0 END) candidates
                    FROM raw_records WHERE subreddit=?""",
                    (subreddit,),
                ).fetchone()
                complete = connection.execute(
                    """SELECT COUNT(*) FROM spells
                    WHERE source_subreddit=? AND completeness_status='COMPLETE'""",
                    (subreddit,),
                ).fetchone()[0]
                connection.execute(
                    """INSERT INTO subreddit_progress
                    (subreddit, stage, posts_processed, comments_processed, candidates_found,
                     complete_found, status, bytes_downloaded, updated_at)
                    VALUES (?, 'analyze', ?, ?, ?, ?, ?, 0, ?)
                    ON CONFLICT(subreddit, stage) DO UPDATE SET
                      posts_processed=excluded.posts_processed,
                      comments_processed=excluded.comments_processed,
                      candidates_found=excluded.candidates_found,
                      complete_found=excluded.complete_found,
                      status=excluded.status,
                      updated_at=excluded.updated_at""",
                    (
                        subreddit,
                        int(counts["posts"] or 0),
                        int(counts["comments"] or 0),
                        int(counts["candidates"] or 0),
                        int(complete),
                        status,
                        utc_now(),
                    ),
                )

    def category_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT category, COUNT(*) AS count FROM spells
                WHERE completeness_status='COMPLETE' GROUP BY category ORDER BY category"""
            ).fetchall()
        return {str(row["category"]): int(row["count"]) for row in rows}

    def record_error(
        self,
        stage: str,
        error: BaseException | str,
        *,
        job_id: str = "",
        subreddit: str = "",
        source_id: str = "",
        retryable: bool = False,
    ) -> int:
        error_type = type(error).__name__ if isinstance(error, BaseException) else "Error"
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO errors
                (job_id, stage, subreddit, source_id, error_type, message, retryable, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    stage,
                    subreddit,
                    source_id,
                    error_type,
                    str(error),
                    int(retryable),
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def unresolved_errors(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM errors WHERE resolved_at='' ORDER BY id"
                ).fetchall()
            ]

    def requeue_retryable_analysis_errors(self) -> int:
        """Zet alleen eerder mislukte analyseraws opnieuw klaar voor `resume`."""

        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id, source_id FROM errors
                WHERE stage='analyze' AND retryable=1 AND resolved_at=''"""
            ).fetchall()
            for row in rows:
                connection.execute(
                    """UPDATE raw_records SET processed_at=''
                    WHERE platform='reddit' AND source_id=?""",
                    (row["source_id"],),
                )
                connection.execute(
                    "UPDATE errors SET resolved_at=? WHERE id=?",
                    (utc_now(), row["id"]),
                )
        return len(rows)

    def requeue_spells_for_analysis(self, statuses: tuple[str, ...]) -> int:
        if not statuses:
            return 0
        allowed = {"COMPLETE", "INCOMPLETE", "REVIEW", "DUPLICATE"}
        if not set(statuses) <= allowed:
            raise ValueError("Ongeldige heranalysestatus")
        placeholders = ",".join("?" for _ in statuses)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT DISTINCT ss.raw_record_id
                FROM spell_sources ss JOIN spells s ON s.unique_id=ss.spell_id
                WHERE s.completeness_status IN ({placeholders})""",
                statuses,
            ).fetchall()
            ids = [int(row["raw_record_id"]) for row in rows]
            if ids:
                id_placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"UPDATE raw_records SET processed_at='' WHERE id IN ({id_placeholders})",
                    ids,
                )
        return len(ids)

    def reset_analysis(self) -> None:
        """Wis alleen afgeleide analyses; ruwe bronrecords en jobs blijven behouden."""

        with self.connect() as connection:
            connection.execute("DELETE FROM duplicate_links")
            connection.execute("DELETE FROM spell_evidence")
            connection.execute("DELETE FROM spell_sources")
            connection.execute("DELETE FROM publication_permissions")
            connection.execute("DELETE FROM spells")
            connection.execute("DELETE FROM analysis_runs")
            connection.execute("DELETE FROM subreddit_progress WHERE stage='analyze'")
            connection.execute(
                "UPDATE raw_records SET is_candidate=0, processed_at=''"
            )

    def approve_publication(
        self,
        spell_id: str,
        *,
        permission_scope: str,
        rights_status: str,
        rights_holder_reference: str = "",
        evidence_reference: str = "",
    ) -> None:
        if rights_status not in {"PERMISSION_GRANTED", "PUBLIC_DOMAIN"}:
            raise ValueError("rights_status moet PERMISSION_GRANTED of PUBLIC_DOMAIN zijn")
        with self.connect() as connection:
            spell = connection.execute(
                "SELECT completeness_status, safety_status FROM spells WHERE unique_id=?",
                (spell_id,),
            ).fetchone()
            if spell is None:
                raise KeyError(f"Onbekende spell-ID: {spell_id}")
            if spell["completeness_status"] != "COMPLETE" or spell["safety_status"] != "SAFE":
                raise ValueError(
                    "Alleen complete, veilig beoordeelde records kunnen worden goedgekeurd"
                )
            connection.execute(
                """INSERT INTO publication_permissions
                (spell_id, permission_scope, rights_holder_reference, evidence_reference,
                 status, granted_at, created_at)
                VALUES (?, ?, ?, ?, 'GRANTED', ?, ?)
                ON CONFLICT(spell_id, permission_scope) DO UPDATE SET
                  rights_holder_reference=excluded.rights_holder_reference,
                  evidence_reference=excluded.evidence_reference,
                  status='GRANTED', granted_at=excluded.granted_at""",
                (
                    spell_id,
                    permission_scope,
                    rights_holder_reference,
                    evidence_reference,
                    utc_now(),
                    utc_now(),
                ),
            )
            connection.execute(
                """UPDATE spells SET rights_status=?, review_status='APPROVED',
                publication_status='APPROVED', publication_permission=1, updated_at=?
                WHERE unique_id=?""",
                (rights_status, utc_now(), spell_id),
            )

    def add_removal_request(
        self,
        *,
        source_id: str,
        spell_id: str | None = None,
        requester_reference: str = "",
        reason: str = "",
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO removal_requests
                (spell_id, source_id, requester_reference, reason, requested_at)
                VALUES (?, ?, ?, ?, ?)""",
                (spell_id, source_id, requester_reference, reason, utc_now()),
            )
            if spell_id:
                connection.execute(
                    """UPDATE spells SET removal_requested=1, rights_status='REMOVAL_REQUESTED',
                    publication_status='EXCLUDED', review_status='NEEDS_REVIEW', updated_at=?
                    WHERE unique_id=?""",
                    (utc_now(), spell_id),
                )
            if source_id:
                connection.execute(
                    """UPDATE spells SET removal_requested=1, rights_status='REMOVAL_REQUESTED',
                    publication_status='EXCLUDED', review_status='NEEDS_REVIEW', updated_at=?
                    WHERE source_post_id=? OR source_comment_ids_json LIKE ?""",
                    (utc_now(), source_id, f'%"{source_id}"%'),
                )
            return int(cursor.lastrowid)

    def add_evidence(self, evidence: SpellEvidence) -> int:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO spell_evidence (
                    spell_id, field_name, raw_record_id, quote, start_offset,
                    end_offset, evidence_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(spell_id, field_name, raw_record_id, quote) DO UPDATE SET
                    start_offset=excluded.start_offset, end_offset=excluded.end_offset,
                    evidence_type=excluded.evidence_type
                """,
                (
                    evidence.spell_id,
                    evidence.field_name,
                    evidence.raw_record_id,
                    evidence.quote,
                    evidence.start_offset,
                    evidence.end_offset,
                    evidence.evidence_type,
                    utc_now(),
                ),
            )
            row = connection.execute(
                """SELECT id FROM spell_evidence
                WHERE spell_id=? AND field_name=? AND raw_record_id=? AND quote=?""",
                (
                    evidence.spell_id,
                    evidence.field_name,
                    evidence.raw_record_id,
                    evidence.quote,
                ),
            ).fetchone()
            return int(row["id"])

    def add_spell_source(self, spell_id: str, raw_record_id: int, role: str = "primary") -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO spell_sources
                (spell_id, raw_record_id, source_role, created_at) VALUES (?, ?, ?, ?)""",
                (spell_id, raw_record_id, role, utc_now()),
            )

    def add_duplicate(
        self,
        master_spell_id: str,
        duplicate_spell_id: str,
        method: str,
        similarity: float,
        notes: str = "",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO duplicate_links
                (master_spell_id, duplicate_spell_id, method, similarity, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(master_spell_id, duplicate_spell_id, method) DO UPDATE SET
                    similarity=excluded.similarity, notes=excluded.notes""",
                (master_spell_id, duplicate_spell_id, method, similarity, notes, utc_now()),
            )

    def save_checkpoint(
        self,
        stage: str,
        subreddit: str = "",
        *,
        cursor: dict[str, Any] | None = None,
        last_source_id: str = "",
        records_processed: int = 0,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO checkpoints
                (stage, subreddit, cursor_json, last_source_id, records_processed, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(stage, subreddit) DO UPDATE SET
                    cursor_json=excluded.cursor_json, last_source_id=excluded.last_source_id,
                    records_processed=excluded.records_processed, updated_at=excluded.updated_at""",
                (
                    stage,
                    subreddit,
                    _json(cursor or {}),
                    last_source_id,
                    records_processed,
                    utc_now(),
                ),
            )

    def get_checkpoint(self, stage: str, subreddit: str = "") -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE stage=? AND subreddit=?", (stage, subreddit)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["cursor"] = json.loads(result.pop("cursor_json"))
        return result

    def statistics(self) -> DatabaseStats:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM raw_records WHERE source_kind='post') AS posts,
                    (SELECT COUNT(*) FROM raw_records WHERE source_kind='comment') AS comments,
                    (SELECT COUNT(*) FROM raw_records WHERE processed_at <> '') AS analyzed_records,
                    (SELECT COUNT(*) FROM raw_records WHERE is_candidate=1) AS potential_spells,
                    (SELECT COUNT(*) FROM spells
                        WHERE completeness_status='COMPLETE') AS complete_spells,
                    (SELECT COUNT(*) FROM spells
                        WHERE completeness_status='INCOMPLETE') AS incomplete_spells,
                    (SELECT COUNT(*) FROM spells
                        WHERE completeness_status='DUPLICATE') AS duplicate_spells,
                    (SELECT COUNT(*) FROM spells WHERE completeness_status='REVIEW'
                        OR review_status IN ('PENDING', 'NEEDS_REVIEW')) AS review_spells,
                    (SELECT COUNT(*) FROM spells
                        WHERE safety_status IN ('FLAGGED', 'EXCLUDED')) AS safety_flagged
                """
            ).fetchone()
        return DatabaseStats(**dict(row))


@contextmanager
def open_database(path: str | Path) -> Iterator[Database]:
    """Initialize and yield a database facade for a bounded unit of work."""

    database = Database(path)
    database.initialize()
    yield database
