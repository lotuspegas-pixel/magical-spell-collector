"""Typed records shared by ingestion, analysis, persistence and exports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class CompletenessStatus(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    REVIEW = "REVIEW"
    DUPLICATE = "DUPLICATE"


class ReviewStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class RightsStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    RESTRICTED = "RESTRICTED"
    PERMISSION_GRANTED = "PERMISSION_GRANTED"
    PUBLIC_DOMAIN = "PUBLIC_DOMAIN"
    REMOVAL_REQUESTED = "REMOVAL_REQUESTED"


class SafetyStatus(StrEnum):
    UNCHECKED = "UNCHECKED"
    SAFE = "SAFE"
    FLAGGED = "FLAGGED"
    EXCLUDED = "EXCLUDED"


class PublicationStatus(StrEnum):
    NOT_READY = "NOT_READY"
    ELIGIBLE = "ELIGIBLE"
    APPROVED = "APPROVED"
    EXCLUDED = "EXCLUDED"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class RecordKind(StrEnum):
    POST = "post"
    COMMENT = "comment"


@dataclass(slots=True)
class RawRecord:
    source_id: str
    source_kind: RecordKind | str
    subreddit: str
    title: str = ""
    body: str = ""
    platform: str = "reddit"
    parent_id: str = ""
    link_id: str = ""
    author: str = ""
    created_utc: str = ""
    permalink: str = ""
    source_url: str = ""
    raw_json: dict[str, Any] | str = field(default_factory=dict)
    content_hash: str = ""
    thread_key: str = ""
    ingestion_job_id: str = ""
    is_candidate: bool = False
    processed_at: str = ""
    date_collected: str = field(default_factory=utc_now)

    @property
    def text(self) -> str:
        return "\n\n".join(part for part in (self.title.strip(), self.body.strip()) if part)


@dataclass(slots=True)
class SpellRecord:
    unique_id: str
    title: str
    title_original: str = ""
    language: str = ""
    category: str = ""
    categories: list[str] = field(default_factory=list)
    subcategories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    purpose: str = ""
    materials: list[str] = field(default_factory=list)
    preparation: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)
    incantation: str = ""
    timing: str = ""
    closing_steps: list[str] = field(default_factory=list)
    historical_tradition: str = ""
    symbolism: str = ""
    source_subreddit: str = ""
    source_url: str = ""
    source_post_id: str = ""
    source_comment_ids: list[str] = field(default_factory=list)
    source_date: str = ""
    source_author: str = ""
    original_text_reference: str = ""
    structured_interpretation_nl: str = ""
    completeness_status: CompletenessStatus | str = CompletenessStatus.REVIEW
    completeness_score: float = 0.0
    extraction_confidence: float = 0.0
    safety_notes: list[str] = field(default_factory=list)
    rights_status: RightsStatus | str = RightsStatus.UNKNOWN
    review_status: ReviewStatus | str = ReviewStatus.PENDING
    date_collected: str = field(default_factory=utc_now)
    safety_status: SafetyStatus | str = SafetyStatus.UNCHECKED
    publication_status: PublicationStatus | str = PublicationStatus.NOT_READY
    rights_notes: str = ""
    analysis_method: str = "heuristic"
    analysis_version: str = "1"
    normalized_hash: str = ""
    simhash: str = ""
    removal_requested: bool = False
    publication_permission: bool = False


@dataclass(slots=True, frozen=True)
class SpellEvidence:
    spell_id: str
    field_name: str
    raw_record_id: int
    quote: str
    start_offset: int | None = None
    end_offset: int | None = None
    evidence_type: str = "source"


@dataclass(slots=True, frozen=True)
class DatabaseStats:
    posts: int = 0
    comments: int = 0
    analyzed_records: int = 0
    potential_spells: int = 0
    complete_spells: int = 0
    incomplete_spells: int = 0
    duplicate_spells: int = 0
    review_spells: int = 0
    safety_flagged: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "posts": self.posts,
            "comments": self.comments,
            "analyzed_records": self.analyzed_records,
            "potential_spells": self.potential_spells,
            "complete_spells": self.complete_spells,
            "incomplete_spells": self.incomplete_spells,
            "duplicate_spells": self.duplicate_spells,
            "review_spells": self.review_spells,
            "safety_flagged": self.safety_flagged,
        }
