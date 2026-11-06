"""Brongebonden, conservatieve spreukextractie zonder ontbrekende delen te verzinnen."""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from spell_collector.dedupe import content_hash, normalize_text, simhash64
from spell_collector.safety import assess_safety

_RITUAL_TERMS = {
    "spell",
    "ritual",
    "incantation",
    "invocation",
    "enchantment",
    "charm",
    "talisman",
    "sigil",
    "blessing",
    "cleansing",
    "protection",
    "prosperity",
    "candle magic",
    "moon magic",
    "herbal magic",
    "banishing",
    "binding",
    "warding",
    "hex",
    "curse",
    "grimoire",
    "folk magic",
    "spell jar",
    "spellwork",
    "altar",
    "spreuk",
    "ritueel",
    "bezwering",
    "zegen",
    "reiniging",
    "bescherming",
    "hechizo",
    "encantamiento",
    "sortilège",
    "rituel",
    "zauber",
}

_ACTION_VERBS = {
    "add",
    "anoint",
    "bind",
    "burn",
    "bury",
    "carve",
    "chant",
    "cleanse",
    "close",
    "draw",
    "extinguish",
    "fold",
    "hold",
    "invoke",
    "light",
    "mix",
    "open",
    "place",
    "pour",
    "repeat",
    "say",
    "seal",
    "sprinkle",
    "stir",
    "take",
    "tie",
    "visualize",
    "wash",
    "write",
    "steek",
    "plaats",
    "schrijf",
    "zeg",
    "herhaal",
    "meng",
}

_SECTION_NAMES: dict[str, tuple[str, ...]] = {
    "materials": ("materials", "ingredients", "supplies", "you will need", "benodigdheden"),
    "preparation": ("preparation", "prepare", "before you begin", "voorbereiding"),
    "instructions": ("instructions", "directions", "method", "steps", "procedure", "uitvoering"),
    "incantation": ("incantation", "words", "chant", "say", "bezwering", "spreektekst"),
    "timing": ("timing", "when", "moon phase", "planetary hour", "moment", "maanstand"),
    "closing": ("closing", "finish", "afterwards", "disposal", "afronding", "afsluiting"),
}

_CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "Liefde en relaties": (
        "love",
        "lover",
        "relationship",
        "romance",
        "self-love",
        "friendship",
        "reconcile",
    ),
    "Geld en voorspoed": (
        "money",
        "wealth",
        "prosperity",
        "abundance",
        "business",
        "luck",
        "success",
    ),
    "Bescherming": ("protect", "protection", "ward", "shield", "safe", "amulet"),
    "Reiniging": ("cleanse", "cleansing", "purify", "purification", "banish", "new beginning"),
    "Maan- en natuurmagie": ("moon", "lunar", "season", "nature", "element", "solstice", "equinox"),
    "Dromen en intuïtie": ("dream", "lucid", "intuition", "vision", "meditation", "divination"),
    "Kaarsen, kruiden en kristallen": ("candle", "herb", "crystal", "stone", "oil", "incense"),
    "Sigils en symbolen": ("sigil", "rune", "symbol", "talisman", "amulet", "bindrune"),
    "Ceremoniële magie": ("ceremonial", "invocation", "evocation", "grimoire", "thelema", "occult"),
    "Overige tradities": (
        "folklore",
        "ancestor",
        "wicca",
        "pagan",
        "hoodoo",
        "chaos magic",
        "hellenism",
    ),
}


@dataclass(slots=True)
class AnalysisResult:
    unique_id: str
    title: str
    title_original: str
    language: str
    category: str
    categories: list[str]
    subcategories: list[str]
    tags: list[str]
    purpose: str
    materials: list[str]
    preparation: list[str]
    instructions: list[str]
    incantation: str
    timing: str
    closing_steps: list[str]
    historical_tradition: str
    symbolism: str
    source_subreddit: str
    source_url: str
    source_post_id: str
    source_comment_ids: list[str]
    source_date: str
    source_author: str
    original_text_reference: str
    structured_interpretation_nl: str
    completeness_status: str
    completeness_score: float
    extraction_confidence: float
    safety_status: str
    safety_notes: list[str]
    rights_status: str
    review_status: str
    publication_status: str
    date_collected: str
    normalized_hash: str
    simhash: str
    analysis_method: str = "heuristic-v1"
    evidence: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r", "").split("\n")]


def _clean_item(line: str) -> str:
    return re.sub(r"^(?:[-*•]+|\d+[.)]|step\s+\d+[:.)]?)\s*", "", line, flags=re.I).strip()


def _heading_key(line: str) -> str | None:
    compact = re.sub(r"^[#*\s]+|[:*\s]+$", "", line).casefold()
    for key, names in _SECTION_NAMES.items():
        if any(compact == name or compact.startswith(f"{name}:") for name in names):
            return key
    return None


def extract_sections(text: str) -> dict[str, list[str]]:
    sections = {key: [] for key in _SECTION_NAMES}
    current: str | None = None
    for line in _lines(text):
        if not line:
            continue
        key = _heading_key(line)
        if key:
            current = key
            remainder = line.split(":", 1)[1].strip() if ":" in line else ""
            if remainder:
                sections[current].append(remainder)
            continue
        if current:
            # Een nieuwe Markdown-kop beëindigt een onbekende sectie.
            if line.startswith("#") and _heading_key(line) is None:
                current = None
            else:
                sections[current].append(_clean_item(line))
    return {key: [item for item in values if item] for key, values in sections.items()}


def _fallback_instructions(text: str) -> list[str]:
    found: list[str] = []
    for line in _lines(text):
        cleaned = _clean_item(line)
        if not cleaned or len(cleaned) < 8:
            continue
        numbered = bool(re.match(r"^(?:[-*•]+|\d+[.)]|step\s+\d+)", line, flags=re.I))
        first = normalize_text(cleaned).split()[:1]
        action_count = sum(
            len(re.findall(rf"\b{re.escape(verb)}\b", cleaned, flags=re.I))
            for verb in _ACTION_VERBS
        )
        if (
            numbered
            and (first and first[0] in _ACTION_VERBS or action_count)
            or first
            and first[0] in _ACTION_VERBS
        ):
            found.append(cleaned)
        if len(found) >= 20:
            break
    return found


def _extract_sentences(text: str, pattern: str, limit: int = 3) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [sentence.strip() for sentence in sentences if re.search(pattern, sentence, re.I)][
        :limit
    ]


def detect_language(text: str) -> str:
    tokens = normalize_text(text).split()
    markers = {
        "en": {"the", "and", "with", "you", "your", "then", "this"},
        "nl": {"de", "het", "en", "met", "je", "jouw", "daarna"},
        "es": {"el", "la", "y", "con", "para", "luego", "tu"},
        "de": {"der", "die", "das", "und", "mit", "dann", "dein"},
        "fr": {"le", "la", "et", "avec", "pour", "puis", "votre"},
    }
    scores = {
        language: sum(token in words for token in tokens) for language, words in markers.items()
    }
    language, score = max(scores.items(), key=lambda item: item[1])
    return language if score >= 2 else "unknown"


def candidate_score(title: str, text: str, extra_terms: list[str] | None = None) -> float:
    normalized = normalize_text(f"{title}\n{text}")
    terms = _RITUAL_TERMS | {term.casefold() for term in extra_terms or []}
    term_hits = sum(1 for term in terms if normalize_text(term) in normalized)
    action_hits = sum(
        1 for verb in _ACTION_VERBS if re.search(rf"\b{re.escape(verb)}\b", normalized)
    )
    numbered = len(re.findall(r"(?m)^\s*(?:\d+[.)]|step\s+\d+|[-*•])\s+", text, re.I))
    section_hits = sum(bool(values) for values in extract_sections(text).values())
    score = min(
        100.0, term_hits * 12 + min(action_hits, 8) * 5 + min(numbered, 8) * 4 + section_hits * 6
    )
    if re.search(
        r"\b(?:anyone know|looking for|need a spell|what spell|can someone|is this a spell)\b",
        normalized,
    ):
        score -= 25
    return max(0.0, score)


def classify(
    text: str,
    category_terms: dict[str, tuple[str, ...] | list[str]] | None = None,
) -> tuple[str, list[str], list[str], list[str]]:
    normalized = normalize_text(text)
    scored: list[tuple[str, int, list[str]]] = []
    configured = category_terms or _CATEGORY_TERMS
    for category, terms in configured.items():
        hits = [term for term in terms if normalize_text(term) in normalized]
        if hits:
            scored.append((category, len(hits), hits))
    scored.sort(key=lambda item: (-item[1], item[0]))
    categories = [item[0] for item in scored] or ["Overige tradities"]
    tags = sorted({term for _, _, hits in scored for term in hits})
    subcategories = tags[:8]
    return categories[0], categories, subcategories, tags


def split_rituals(title: str, text: str) -> list[tuple[str, str]]:
    """Splits alleen bij expliciete genummerde/betitelde spell-koppen."""

    pattern = re.compile(
        r"(?im)^(?:#{1,4}\s*)?(?P<title>(?:spell|ritual|charm|rite)\s*(?:#|no\.?\s*)?\d+[^\n:]*(?::[^\n]*)?)\s*$"
    )
    matches = list(pattern.finditer(text))
    if len(matches) < 2:
        return [(title, text)]
    results: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[match.end() : end].strip()
        if segment:
            results.append((match.group("title").strip(" #:"), segment))
    return results or [(title, text)]


def analyze_text(
    *,
    title: str,
    text: str,
    source: dict[str, Any],
    extra_terms: list[str] | None = None,
    category_terms: dict[str, tuple[str, ...] | list[str]] | None = None,
) -> AnalysisResult:
    sections = extract_sections(text)
    instructions = sections["instructions"] or _fallback_instructions(text)
    materials = sections["materials"]
    preparation = sections["preparation"]
    closing = sections["closing"]
    incantation = "\n".join(sections["incantation"])
    timing_parts = sections["timing"] or _extract_sentences(
        text,
        r"\b(?:full moon|new moon|waxing|waning|dawn|sunset|midnight|friday|saturday|solstice|equinox)\b",
    )
    timing = " ".join(timing_parts)
    tradition_parts = _extract_sentences(
        text,
        r"\b(?:wicca|wiccan|hoodoo|folk magic|thelema|hellenism|pagan|chaos magic|ceremonial|grimoire|tradition)\b",
        2,
    )
    symbolism_parts = _extract_sentences(
        text,
        r"\b(?:symboli[sz]e|represents?|associated with|correspondence|element of|colour|color means)\b",
        2,
    )
    purpose_parts = _extract_sentences(
        text,
        r"\b(?:purpose|intent(?:ion)?|this (?:spell|ritual)|used (?:for|to)|designed to|helps? (?:with|to))\b",
        1,
    )
    purpose = purpose_parts[0] if purpose_parts else title.strip()
    relevance = candidate_score(title, text, extra_terms)
    question_only = (
        bool(
            re.search(
                r"\b(?:anyone know|looking for|need a spell|what spell|can someone recommend)\b",
                normalize_text(f"{title} {text}"),
            )
        )
        and not instructions
    )
    uncertain_source = bool(
        re.search(
            r"\b(?:please (?:help|correct)|help me|i am not sure|i'm not sure|"
            r"should i|does this work|will this work|what i(?:'m| am) thinking)\b",
            f"{title} {text}",
            re.I,
        )
    )

    step_actions = sum(
        max(
            1, sum(bool(re.search(rf"\b{re.escape(verb)}\b", step, re.I)) for verb in _ACTION_VERBS)
        )
        for step in instructions
    )
    completeness = 0.0
    completeness += 10 if title.strip() else 0
    completeness += 15 if purpose else 0
    completeness += min(45, step_actions * 15)
    completeness += 10 if materials else 0
    completeness += 5 if preparation else 0
    completeness += 5 if incantation else 0
    completeness += 5 if timing else 0
    completeness += 5 if closing else 0
    completeness = min(100.0, completeness)

    explicit_ritual_title = bool(
        re.search(r"\b(?:spell|ritual|rite|charm|incantation|spreuk|ritueel)\b", title, re.I)
    )
    executable_explicit_ritual = (
        explicit_ritual_title
        and step_actions >= 3
        and len(instructions) >= 2
        and completeness >= 65
        and relevance >= 35
    )
    if question_only or relevance < 35:
        status = "INCOMPLETE"
    elif uncertain_source:
        status = "REVIEW"
    elif (step_actions >= 2 and completeness >= 65 and relevance >= 50) or executable_explicit_ritual:
        status = "COMPLETE"
    elif completeness >= 50 or relevance >= 55:
        status = "REVIEW"
    else:
        status = "INCOMPLETE"

    category, categories, subcategories, tags = classify(
        f"{title}\n{text}", category_terms=category_terms
    )
    safety = assess_safety(f"{title}\n{text}")
    review_status = (
        "PENDING" if status == "REVIEW" or safety.status != "NO_RISK_DETECTED" else "AUTO_REVIEWED"
    )
    publication_status = "BLOCKED_RIGHTS"
    if safety.status != "NO_RISK_DETECTED":
        publication_status = "BLOCKED_SAFETY"

    original_title = title.strip()
    display_title = (
        original_title.rstrip("?") or f"Ritueel uit r/{source.get('subreddit', 'onbekend')}"
    )
    if question_only:
        display_title = f"Kandidaat: {display_title}"
    confidence = max(0.0, min(1.0, (relevance / 100) * 0.45 + (completeness / 100) * 0.55))
    collected = source.get("date_collected") or datetime.now(UTC).isoformat()
    combined = "\n".join([title, text, *source.get("supplement_texts", [])])

    evidence = {
        "purpose": [purpose] if purpose else [],
        "materials": materials,
        "preparation": preparation,
        "instructions": instructions,
        "incantation": sections["incantation"],
        "timing": timing_parts,
        "closing_steps": closing,
        "historical_tradition": tradition_parts,
        "symbolism": symbolism_parts,
    }
    return AnalysisResult(
        unique_id=str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"reddit:{source.get('post_id', source.get('id', ''))}:{content_hash(text)}",
            )
        ),
        title=display_title,
        title_original=original_title,
        language=detect_language(combined),
        category=category,
        categories=categories,
        subcategories=subcategories,
        tags=tags,
        purpose=purpose,
        materials=materials,
        preparation=preparation,
        instructions=instructions,
        incantation=incantation,
        timing=timing,
        closing_steps=closing,
        historical_tradition=" ".join(tradition_parts),
        symbolism=" ".join(symbolism_parts),
        source_subreddit=source.get("subreddit", ""),
        source_url=source.get("url", ""),
        source_post_id=source.get("post_id", source.get("id", "")),
        source_comment_ids=list(source.get("comment_ids", [])),
        source_date=str(source.get("created_utc", "")),
        source_author=source.get("author", ""),
        original_text_reference=f"raw:{source.get('raw_id', source.get('id', ''))}",
        structured_interpretation_nl=(
            f"De bron beschrijft een rituele handeling binnen de categorie {category}. "
            "Dit is een broninterpretatie; genoemde magische effecten zijn niet wetenschappelijk bewezen."
        ),
        completeness_status=status,
        completeness_score=round(completeness, 2),
        extraction_confidence=round(confidence, 4),
        safety_status=safety.status,
        safety_notes=safety.notes,
        rights_status="UNKNOWN_REDDIT_AUTHOR_COPYRIGHT",
        review_status=review_status,
        publication_status=publication_status,
        date_collected=collected,
        normalized_hash=content_hash(combined),
        simhash=str(simhash64(combined)),
        evidence=evidence,
    )
