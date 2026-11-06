"""Deterministische en fuzzy tekstontdubbeling."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from rapidfuzz.fuzz import ratio, token_set_ratio

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DuplicateAssessment:
    relation: str
    score: float
    method: str


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"\bu/[a-z0-9_-]+", " ", value)
    value = re.sub(r"\br/[a-z0-9_-]+", " ", value)
    return " ".join(_TOKEN_RE.findall(value))


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def simhash64(text: str) -> int:
    tokens = normalize_text(text).split()
    if not tokens:
        return 0
    vector = [0] * 64
    for token in tokens:
        hashed = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            vector[bit] += 1 if hashed & (1 << bit) else -1
    value = 0
    for bit, weight in enumerate(vector):
        if weight >= 0:
            value |= 1 << bit
    return value


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    left_set = {normalize_text(value) for value in left if normalize_text(value)}
    right_set = {normalize_text(value) for value in right if normalize_text(value)}
    if not left_set and not right_set:
        return 1.0
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def compare_texts(left: str, right: str) -> DuplicateAssessment:
    left_norm, right_norm = normalize_text(left), normalize_text(right)
    if left_norm == right_norm and left_norm:
        return DuplicateAssessment("DUPLICATE", 1.0, "normalized_sha256")
    if not left_norm or not right_norm:
        return DuplicateAssessment("DISTINCT", 0.0, "empty")

    ordered = ratio(left_norm, right_norm) / 100
    token_score = token_set_ratio(left_norm, right_norm) / 100
    sim_score = 1 - hamming_distance(simhash64(left_norm), simhash64(right_norm)) / 64
    score = 0.45 * token_score + 0.35 * ordered + 0.20 * sim_score
    length_ratio = min(len(left_norm), len(right_norm)) / max(len(left_norm), len(right_norm))

    if score >= 0.93 and length_ratio >= 0.75:
        return DuplicateAssessment("DUPLICATE", round(score, 4), "fuzzy_text")
    if score >= 0.82 and length_ratio >= 0.6:
        return DuplicateAssessment("REVIEW", round(score, 4), "fuzzy_text")
    return DuplicateAssessment("DISTINCT", round(score, 4), "fuzzy_text")
