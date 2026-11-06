"""Hervatbare acquisitie-, ingestie- en analysepipeline."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from spell_collector.analyzer import (
    AnalysisResult,
    analyze_text,
    candidate_score,
    split_rituals,
)
from spell_collector.config import AppConfig
from spell_collector.db import Database
from spell_collector.dedupe import compare_texts, content_hash
from spell_collector.models import RawRecord, RecordKind, SpellEvidence, SpellRecord
from spell_collector.ollama import OllamaAnalyzer, OllamaUnavailable
from spell_collector.sources.arctic_cli import ArcticCLI
from spell_collector.sources.arctic_shift import ArcticShiftClient, ArcticShiftConfig


class StorageBudgetError(RuntimeError):
    pass


@dataclass(slots=True)
class CollectionOutcome:
    job_id: str
    source_route: str
    records_received: int
    records_inserted: int
    posts: int
    comments: int
    bytes_used: int
    arctic_route: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "source_route": self.source_route,
            "records_received": self.records_received,
            "records_inserted": self.records_inserted,
            "posts": self.posts,
            "comments": self.comments,
            "bytes_used": self.bytes_used,
            "arctic_route": self.arctic_route,
        }


@dataclass(slots=True)
class AnalysisOutcome:
    job_id: str
    analyzed_records: int = 0
    candidates: int = 0
    spells_written: int = 0
    complete: int = 0
    incomplete: int = 0
    review: int = 0
    duplicates: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "analyzed_records": self.analyzed_records,
            "candidates": self.candidates,
            "spells_written": self.spells_written,
            "complete": self.complete,
            "incomplete": self.incomplete,
            "review": self.review,
            "duplicates": self.duplicates,
            "errors": self.errors,
        }


def directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def check_storage_budget(config: AppConfig, *, expected_bytes: int = 0) -> dict[str, int]:
    config.ensure_directories()
    used = directory_size(config.storage.data_dir)
    maximum = int(config.storage.max_gb * 1024**3)
    reserve = int(config.storage.reserve_free_gb * 1024**3)
    free = shutil.disk_usage(config.storage.data_dir).free
    if used + expected_bytes > maximum:
        raise StorageBudgetError(
            f"Projectopslag zou {used + expected_bytes} bytes worden; limiet is {maximum}."
        )
    if free - expected_bytes < reserve:
        raise StorageBudgetError(f"Vrije ruimte zou onder de reserve van {reserve} bytes komen.")
    return {
        "used_bytes": used,
        "max_bytes": maximum,
        "free_bytes": free,
        "reserve_bytes": reserve,
        "remaining_project_bytes": maximum - used,
    }


def _bare_id(value: Any) -> str:
    text = str(value or "").strip()
    return text[3:] if text.startswith(("t1_", "t3_")) else text


def _kind(item: Mapping[str, Any]) -> str:
    value = str(item.get("kind") or item.get("type") or "").casefold()
    if value in {"comment", "comments", "rc"} or (
        item.get("body") is not None and not item.get("title")
    ):
        return "comment"
    return "post"


def _reddit_url(item: Mapping[str, Any], kind: str, source_id: str, post_id: str) -> str:
    candidate = str(item.get("permalink") or item.get("source_url") or item.get("url") or "")
    if candidate.startswith("/"):
        return f"https://www.reddit.com{candidate}"
    if "reddit.com/" in candidate:
        return candidate
    subreddit = str(item.get("subreddit") or "")
    if kind == "comment" and post_id:
        return f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/_/{source_id}/"
    return f"https://www.reddit.com/r/{subreddit}/comments/{source_id}/"


def normalise_raw_record(
    item: Mapping[str, Any],
    *,
    job_id: str,
    retain_author: bool,
) -> RawRecord:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else dict(item)
    kind = _kind(item)
    source_id = _bare_id(item.get("id") or item.get("source_id"))
    if not source_id:
        raise ValueError("Bronrecord mist een Reddit-ID")
    link_id = _bare_id(item.get("source_post_id") or item.get("link_id"))
    post_id = source_id if kind == "post" else link_id
    parent_id = _bare_id(item.get("source_parent_id") or item.get("parent_id"))
    title = str(item.get("title") or "") if kind == "post" else ""
    body = str(
        item.get("text")
        if item.get("text") is not None
        else item.get("selftext")
        if kind == "post"
        else item.get("body") or ""
    )
    created = item.get("created_utc") or item.get("created_at") or ""
    source_meta = item.get("source") if isinstance(item.get("source"), dict) else {}
    return RawRecord(
        source_id=source_id,
        source_kind=RecordKind.POST if kind == "post" else RecordKind.COMMENT,
        subreddit=str(item.get("subreddit") or ""),
        title=title,
        body=body,
        parent_id=parent_id,
        link_id=post_id,
        author=str(item.get("author") or "") if retain_author else "",
        created_utc=str(created),
        permalink=str(item.get("permalink") or ""),
        source_url=_reddit_url(item, kind, source_id, post_id),
        raw_json={"record": raw, "collection_source": source_meta},
        content_hash=content_hash(f"{title}\n{body}"),
        thread_key=post_id,
        ingestion_job_id=job_id,
        date_collected=str(source_meta.get("collected_at") or datetime.now(UTC).isoformat()),
    )


def ingest_records(
    database: Database,
    records: Iterable[Mapping[str, Any]],
    *,
    job_id: str,
    config: AppConfig,
) -> CollectionOutcome:
    before = directory_size(config.storage.data_dir)
    received = inserted = posts = comments = 0
    route = "unknown"
    for item in records:
        received += 1
        check_storage_budget(config)
        try:
            record = normalise_raw_record(
                item,
                job_id=job_id,
                retain_author=config.rights.retain_source_author,
            )
            source = item.get("source")
            if isinstance(source, dict):
                route = str(source.get("route") or route)
            database.upsert_raw_record(record)
            inserted += 1
            if str(record.source_kind) == RecordKind.POST.value:
                posts += 1
            else:
                comments += 1
        except Exception as exc:
            database.record_error(
                "ingest",
                exc,
                job_id=job_id,
                subreddit=str(item.get("subreddit") or ""),
                source_id=str(item.get("id") or ""),
            )
    after = directory_size(config.storage.data_dir)
    return CollectionOutcome(
        job_id=job_id,
        source_route=route,
        records_received=received,
        records_inserted=inserted,
        posts=posts,
        comments=comments,
        bytes_used=max(0, after - before),
    )


def load_jsonl_fixture(path: Path, *, max_records: int) -> Iterator[dict[str, Any]]:
    if max_records < 1 or max_records > 100:
        raise ValueError("Een fixturepilot moet tussen 1 en 100 records bevatten")
    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index >= max_records:
                break
            if line.strip():
                item = json.loads(line)
                item.setdefault(
                    "source",
                    {
                        "provider": "fixture",
                        "route": "offline_fixture",
                        "endpoint": str(path),
                        "collected_at": datetime.now(UTC).isoformat(),
                    },
                )
                yield item


def collect_pilot(
    database: Database,
    config: AppConfig,
    *,
    max_records: int = 100,
    source: str = "auto",
    after: str | None = None,
    before: str | None = None,
    fixture: Path | None = None,
    arctic_binary: str = "arctic",
) -> CollectionOutcome:
    if not 1 <= max_records <= 100:
        raise ValueError("Pilotlimiet moet tussen 1 en 100 liggen")
    if source not in {"auto", "arctic", "api", "fixture"}:
        raise ValueError("source moet auto, arctic, api of fixture zijn")
    check_storage_budget(config, expected_bytes=16 * 1024 * 1024)
    job_id = database.create_job(
        "pilot",
        status="RUNNING",
        config={
            "max_records": max_records,
            "source": source,
            "after": after,
            "before": before,
            "fixture": str(fixture) if fixture else "",
        },
    )
    try:
        if source == "fixture" or fixture is not None:
            if fixture is None:
                raise ValueError("--fixture is verplicht voor source=fixture")
            outcome = ingest_records(
                database,
                load_jsonl_fixture(fixture, max_records=max_records),
                job_id=job_id,
                config=config,
            )
        elif source in {"auto", "arctic"}:
            try:
                outcome = _pilot_with_arctic(
                    database,
                    config,
                    job_id=job_id,
                    max_records=max_records,
                    after=after,
                    before=before,
                    arctic_binary=arctic_binary,
                )
            except Exception:
                if source == "arctic":
                    raise
                outcome = _pilot_with_api(
                    database,
                    config,
                    job_id=job_id,
                    max_records=max_records,
                    after=after,
                    before=before,
                )
        else:
            outcome = _pilot_with_api(
                database,
                config,
                job_id=job_id,
                max_records=max_records,
                after=after,
                before=before,
            )
        database.update_progress(
            config.collection.pilot_subreddit,
            "collect",
            posts=outcome.posts,
            comments=outcome.comments,
            bytes_downloaded=outcome.bytes_used,
            status="COMPLETE",
        )
        database.update_job(job_id, status="COMPLETE", records_processed=outcome.records_inserted)
        return outcome
    except KeyboardInterrupt:
        database.update_job(job_id, status="PAUSED", error_message="Onderbroken door gebruiker")
        raise
    except Exception as exc:
        database.record_error("collect", exc, job_id=job_id, retryable=True)
        database.update_job(job_id, status="FAILED", error_message=str(exc))
        raise


def _default_window(after: str | None, before: str | None) -> tuple[str, str]:
    if after and before:
        return after, before
    end = date.today()
    start = end - timedelta(days=7)
    return after or start.isoformat(), before or end.isoformat()


def _pilot_with_api(
    database: Database,
    config: AppConfig,
    *,
    job_id: str,
    max_records: int,
    after: str | None,
    before: str | None,
) -> CollectionOutcome:
    after, before = _default_window(after, before)
    client = ArcticShiftClient(
        ArcticShiftConfig(polite_delay_seconds=config.collection.api_delay_seconds)
    )
    result = client.collect_pilot(
        config.collection.pilot_subreddit,
        max_records=max_records,
        sort="desc",
        after=after,
        before=before,
    )
    return ingest_records(database, result.records, job_id=job_id, config=config)


def _pilot_with_arctic(
    database: Database,
    config: AppConfig,
    *,
    job_id: str,
    max_records: int,
    after: str | None,
    before: str | None,
    arctic_binary: str,
) -> CollectionOutcome:
    after, before = _default_window(after, before)
    arctic_data = config.storage.raw_dir / "arctic"
    wrapper = ArcticCLI(
        binary=arctic_binary,
        data_dir=arctic_data,
        logs_dir=config.storage.log_dir / "arctic",
    )
    budget = check_storage_budget(config)
    acquisition = wrapper.acquire_subreddit(
        config.collection.pilot_subreddit,
        after=after,
        before=before,
        max_bytes=budget["max_bytes"],
        min_free_bytes=budget["reserve_bytes"],
        kind="both",
        max_window_days=31,
    )
    records = wrapper.query_jsonl(
        config.collection.pilot_subreddit,
        limit=max_records,
        kind="both",
        after=after,
        before=before,
    )
    outcome = ingest_records(database, records, job_id=job_id, config=config)
    outcome.source_route = "arctic_cli"
    outcome.arctic_route = acquisition.route.value
    return outcome


def collect_subreddits(
    database: Database,
    config: AppConfig,
    *,
    subreddits: Iterable[str],
    after: str,
    before: str,
    max_records_per_subreddit: int,
    arctic_binary: str = "arctic",
) -> list[CollectionOutcome]:
    """Verzamel via Arctic CLI, per subreddit en maximaal 31 dagen per netwerktaak."""

    start, end = date.fromisoformat(after), date.fromisoformat(before)
    if end <= start:
        raise ValueError("before moet na after liggen")
    if max_records_per_subreddit < 0:
        raise ValueError("max_records_per_subreddit mag niet negatief zijn")
    outcomes: list[CollectionOutcome] = []
    for subreddit in subreddits:
        job_id = database.create_job(
            "collect",
            status="RUNNING",
            config={
                "subreddit": subreddit,
                "after": after,
                "before": before,
                "max_records": max_records_per_subreddit,
            },
        )
        try:
            window_start = start
            wrapper = ArcticCLI(
                binary=arctic_binary,
                data_dir=config.storage.raw_dir / "arctic",
                logs_dir=config.storage.log_dir / "arctic",
            )
            route = ""
            while window_start < end:
                check_storage_budget(config)
                window_end = min(end, window_start + timedelta(days=31))
                budget = check_storage_budget(config)
                acquisition = wrapper.acquire_subreddit(
                    subreddit,
                    after=window_start,
                    before=window_end,
                    max_bytes=budget["max_bytes"],
                    min_free_bytes=budget["reserve_bytes"],
                    kind=config.collection.kinds,
                    max_window_days=31,
                )
                route = acquisition.route.value
                window_start = window_end

            records = wrapper.query_jsonl(
                subreddit,
                limit=max_records_per_subreddit,
                allow_unlimited=max_records_per_subreddit == 0,
                kind=config.collection.kinds,
                after=start,
                before=end,
                timeout_seconds=3_600,
            )
            outcome = ingest_records(database, records, job_id=job_id, config=config)
            outcome.source_route = "arctic_cli"
            outcome.arctic_route = route
            outcomes.append(outcome)
            database.update_progress(
                subreddit,
                "collect",
                posts=outcome.posts,
                comments=outcome.comments,
                bytes_downloaded=outcome.bytes_used,
                status="COMPLETE",
            )
            database.update_job(
                job_id, status="COMPLETE", records_processed=outcome.records_inserted
            )
        except KeyboardInterrupt:
            database.update_job(job_id, status="PAUSED", error_message="Onderbroken door gebruiker")
            raise
        except Exception as exc:
            database.record_error(
                "collect", exc, job_id=job_id, subreddit=subreddit, retryable=True
            )
            database.update_job(job_id, status="FAILED", error_message=str(exc))
            raise
    return outcomes


_SUPPLEMENT_CUES = (
    "forgot to add",
    "additional step",
    "one more step",
    "you will also need",
    "also add",
    "op here",
    "correction:",
)


def _supplements(database: Database, post: Mapping[str, Any]) -> list[dict[str, Any]]:
    author = str(post.get("author") or "").casefold()
    if not author or author in {"[deleted]", "deleted"}:
        return []
    selected = []
    for comment in database.linked_comments(str(post["source_id"])):
        body = str(comment.get("body") or "")
        same_author = str(comment.get("author") or "").casefold() == author
        has_cue = any(cue in body.casefold() for cue in _SUPPLEMENT_CUES)
        if same_author and has_cue:
            selected.append(comment)
    return selected


def _spell_text(spell: Mapping[str, Any]) -> str:
    values = [
        str(spell.get("title") or ""),
        str(spell.get("purpose") or ""),
        *[str(item) for item in spell.get("materials", [])],
        *[str(item) for item in spell.get("instructions", [])],
        str(spell.get("incantation") or ""),
    ]
    return "\n".join(value for value in values if value)


def _merge_ai(result: AnalysisResult, source_text: str, ollama: OllamaAnalyzer) -> AnalysisResult:
    ai = ollama.analyze(source_text)
    if not ai.spells:
        return result
    first = ai.spells[0]
    result.analysis_method = f"heuristic-v1+ollama:{ollama.model}"
    result.extraction_confidence = round(
        min(1.0, (result.extraction_confidence + float(first.get("confidence", 0))) / 2), 4
    )
    if not ai.validation_errors:
        for field in (
            "purpose",
            "materials",
            "preparation",
            "instructions",
            "incantation",
            "timing",
            "closing_steps",
        ):
            value = first.get(field)
            if value:
                setattr(result, field, value)
        if first.get("title") and not result.title_original:
            result.title = str(first["title"])
        if first.get("description_nl"):
            result.structured_interpretation_nl = str(first["description_nl"])
        result.completeness_status = str(first.get("status") or result.completeness_status)
        result.completeness_score = float(
            first.get("completeness_score", result.completeness_score)
        )
        for field, excerpts in first.get("evidence", {}).items():
            result.evidence[field] = list(excerpts)
    else:
        result.review_status = "NEEDS_REVIEW"
        if result.completeness_status == "COMPLETE":
            result.completeness_status = "REVIEW"
    return result


def _to_spell_record(result: AnalysisResult, config: AppConfig) -> SpellRecord:
    if result.safety_status == "NO_RISK_DETECTED":
        safety_status = "SAFE"
        publication_status = "NOT_READY"
    elif result.safety_status == "DANGEROUS":
        safety_status = "EXCLUDED"
        publication_status = "EXCLUDED"
    else:
        safety_status = "FLAGGED"
        publication_status = "EXCLUDED"
    return SpellRecord(
        unique_id=result.unique_id,
        title=result.title,
        title_original=result.title_original,
        language=result.language,
        category=result.category,
        categories=result.categories,
        subcategories=result.subcategories,
        tags=result.tags,
        purpose=result.purpose,
        materials=result.materials,
        preparation=result.preparation,
        instructions=result.instructions,
        incantation=result.incantation,
        timing=result.timing,
        closing_steps=result.closing_steps,
        historical_tradition=result.historical_tradition,
        symbolism=result.symbolism,
        source_subreddit=result.source_subreddit,
        source_url=result.source_url,
        source_post_id=result.source_post_id,
        source_comment_ids=result.source_comment_ids,
        source_date=result.source_date,
        source_author=result.source_author,
        original_text_reference=result.original_text_reference,
        structured_interpretation_nl=result.structured_interpretation_nl,
        completeness_status=result.completeness_status,
        completeness_score=result.completeness_score,
        extraction_confidence=result.extraction_confidence,
        safety_notes=result.safety_notes,
        rights_status=config.rights.default_status,
        review_status=result.review_status,
        date_collected=result.date_collected,
        safety_status=safety_status,
        publication_status=publication_status,
        analysis_method=result.analysis_method,
        analysis_version="1",
        normalized_hash=result.normalized_hash,
        simhash=result.simhash,
    )


def _add_evidence(
    database: Database,
    result: AnalysisResult,
    primary: Mapping[str, Any],
    supplements: list[dict[str, Any]],
) -> None:
    source_rows = [dict(primary), *supplements]
    for field_name, quotes in result.evidence.items():
        for quote in quotes:
            if not quote:
                continue
            for row in source_rows:
                haystack = f"{row.get('title', '')}\n{row.get('body', '')}"
                offset = haystack.find(quote)
                if offset >= 0:
                    database.add_evidence(
                        SpellEvidence(
                            spell_id=result.unique_id,
                            field_name=field_name,
                            raw_record_id=int(row["id"]),
                            quote=quote,
                            start_offset=offset,
                            end_offset=offset + len(quote),
                        )
                    )
                    break


def analyze_pending(
    database: Database,
    config: AppConfig,
    *,
    limit: int = 0,
    batch_size: int | None = None,
    use_ai: bool | None = None,
    model: str | None = None,
) -> AnalysisOutcome:
    batch_size = batch_size or config.collection.batch_size
    if batch_size < 1:
        raise ValueError("batch_size moet minimaal 1 zijn")
    if limit < 0:
        raise ValueError("limit mag niet negatief zijn")
    ai_enabled = config.ollama.enabled if use_ai is None else use_ai
    ollama: OllamaAnalyzer | None = None
    if ai_enabled:
        selected_model = model or config.ollama.model
        if not selected_model:
            raise ValueError("Configureer een lokaal Ollama-model of geef --model op")
        ollama = OllamaAnalyzer(
            selected_model,
            base_url=config.ollama.base_url,
            timeout=config.ollama.timeout_seconds,
        )
    job_id = database.create_job(
        "analyze",
        status="RUNNING",
        config={
            "limit": limit,
            "batch_size": batch_size,
            "use_ai": ai_enabled,
            "model": model or "",
        },
    )
    outcome = AnalysisOutcome(job_id=job_id)
    signatures = database.list_spells(include_duplicates=False)
    try:
        while not limit or outcome.analyzed_records < limit:
            remaining = (
                batch_size if not limit else min(batch_size, limit - outcome.analyzed_records)
            )
            rows = database.fetch_unprocessed_raw(limit=remaining)
            if not rows:
                break
            for row in rows:
                try:
                    body = str(row.get("body") or "")
                    title = str(row.get("title") or "")
                    if body.strip().casefold() in {"", "[deleted]", "[removed]"}:
                        database.mark_raw_processed(int(row["id"]), is_candidate=False)
                        outcome.analyzed_records += 1
                        continue
                    supplements = (
                        _supplements(database, row) if row["source_kind"] == "post" else []
                    )
                    supplement_texts = [str(item.get("body") or "") for item in supplements]
                    combined = body
                    if supplement_texts:
                        combined += "\n\n" + "\n\n".join(supplement_texts)
                    if row["source_kind"] == "comment" and not title:
                        parent = database.find_raw_record("post", str(row.get("link_id") or ""))
                        if parent:
                            title = str(parent.get("title") or "")
                    relevance = candidate_score(
                        title if row["source_kind"] == "post" else "",
                        combined,
                        extra_terms=list(config.analysis.search_terms),
                    )
                    # Relevante vragen blijven als INCOMPLETE bewaard; ze mogen
                    # alleen nooit de hoofdcollectie bereiken.
                    candidate_threshold = 10
                    if relevance < candidate_threshold:
                        database.mark_raw_processed(int(row["id"]), is_candidate=False)
                        outcome.analyzed_records += 1
                        continue
                    outcome.candidates += 1
                    database.mark_raw_processed(int(row["id"]), is_candidate=True)
                    for segment_title, segment_text in split_rituals(title, combined):
                        source = {
                            "id": row["source_id"],
                            "raw_id": row["id"],
                            "post_id": row["source_id"]
                            if row["source_kind"] == "post"
                            else row.get("link_id") or row["source_id"],
                            "subreddit": row["subreddit"],
                            "url": row["source_url"],
                            "author": row["author"],
                            "created_utc": row["created_utc"],
                            "date_collected": row["date_collected"],
                            "comment_ids": [item["source_id"] for item in supplements]
                            + ([row["source_id"]] if row["source_kind"] == "comment" else []),
                            "supplement_texts": supplement_texts,
                        }
                        result = analyze_text(
                            title=segment_title,
                            text=segment_text,
                            source=source,
                            extra_terms=list(config.analysis.search_terms),
                            category_terms=config.analysis.categories,
                        )
                        if ollama:
                            try:
                                result = _merge_ai(
                                    result, f"{segment_title}\n{segment_text}", ollama
                                )
                            except OllamaUnavailable as exc:
                                database.record_error(
                                    "ollama",
                                    exc,
                                    job_id=job_id,
                                    subreddit=row["subreddit"],
                                    source_id=row["source_id"],
                                    retryable=True,
                                )
                                result.review_status = "NEEDS_REVIEW"

                        duplicate_master: dict[str, Any] | None = database.find_spell_by_hash(
                            result.normalized_hash
                        )
                        if (
                            duplicate_master is not None
                            and str(duplicate_master["unique_id"]) == result.unique_id
                        ):
                            # Idempotente heranalyse van hetzelfde bronsegment.
                            duplicate_master = None
                        duplicate_score = 1.0 if duplicate_master else 0.0
                        duplicate_method = "normalized_sha256"
                        near_review = False
                        if duplicate_master is None:
                            new_text = _spell_text(result.to_dict())
                            for existing in signatures:
                                if str(existing.get("unique_id") or "") == result.unique_id:
                                    continue
                                assessment = compare_texts(new_text, _spell_text(existing))
                                if assessment.relation == "DUPLICATE":
                                    duplicate_master = existing
                                    duplicate_score = assessment.score
                                    duplicate_method = assessment.method
                                    break
                                if assessment.relation == "REVIEW":
                                    near_review = True
                        if duplicate_master:
                            result.completeness_status = "DUPLICATE"
                            result.review_status = "AUTO_REVIEWED"
                        elif near_review and result.completeness_status == "COMPLETE":
                            result.completeness_status = "REVIEW"
                            result.review_status = "NEEDS_REVIEW"

                        spell = _to_spell_record(result, config)
                        database.upsert_spell(spell)
                        database.add_spell_source(result.unique_id, int(row["id"]), "primary")
                        for supplement in supplements:
                            database.add_spell_source(
                                result.unique_id, int(supplement["id"]), "supplement"
                            )
                        _add_evidence(database, result, row, supplements)
                        if duplicate_master:
                            database.add_duplicate(
                                str(duplicate_master["unique_id"]),
                                result.unique_id,
                                duplicate_method,
                                duplicate_score,
                                "Automatisch gekoppeld; bronvarianten blijven herleidbaar.",
                            )
                        else:
                            signatures.append(
                                spell.__dict__ if hasattr(spell, "__dict__") else result.to_dict()
                            )

                        outcome.spells_written += 1
                        status = result.completeness_status
                        if status == "COMPLETE":
                            outcome.complete += 1
                        elif status == "INCOMPLETE":
                            outcome.incomplete += 1
                        elif status == "DUPLICATE":
                            outcome.duplicates += 1
                        else:
                            outcome.review += 1
                    outcome.analyzed_records += 1
                    database.update_progress(
                        str(row["subreddit"]),
                        "analyze",
                        posts=int(row["source_kind"] == "post"),
                        comments=int(row["source_kind"] == "comment"),
                        candidates=1,
                        complete=outcome.complete,
                        status="RUNNING",
                    )
                    database.save_checkpoint(
                        "analyze",
                        str(row["subreddit"]),
                        cursor={"raw_id": row["id"]},
                        last_source_id=str(row["source_id"]),
                        records_processed=outcome.analyzed_records,
                    )
                except Exception as exc:
                    outcome.errors += 1
                    outcome.analyzed_records += 1
                    database.record_error(
                        "analyze",
                        exc,
                        job_id=job_id,
                        subreddit=str(row.get("subreddit") or ""),
                        source_id=str(row.get("source_id") or ""),
                        retryable=True,
                    )
                    database.mark_raw_processed(int(row["id"]), is_candidate=True)
            database.update_job(
                job_id,
                status="RUNNING",
                records_processed=outcome.analyzed_records,
                cursor={"analyzed": outcome.analyzed_records},
            )
            check_storage_budget(config)
        database.rebuild_analysis_progress(status="COMPLETE")
        database.update_job(job_id, status="COMPLETE", records_processed=outcome.analyzed_records)
        return outcome
    except KeyboardInterrupt:
        database.rebuild_analysis_progress(status="PAUSED")
        database.update_job(
            job_id,
            status="PAUSED",
            records_processed=outcome.analyzed_records,
            cursor={"analyzed": outcome.analyzed_records},
            error_message="Onderbroken door gebruiker",
        )
        raise
    except Exception as exc:
        database.rebuild_analysis_progress(status="FAILED")
        database.record_error("analyze", exc, job_id=job_id, retryable=True)
        database.update_job(
            job_id,
            status="FAILED",
            records_processed=outcome.analyzed_records,
            error_message=str(exc),
        )
        raise
