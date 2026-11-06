"""Conservatieve veiligheidscontrole voor rituele instructies."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SafetyAssessment:
    status: str
    notes: list[str]
    publication_allowed: bool


_RULES: tuple[tuple[str, str, str], ...] = (
    (
        "DANGEROUS",
        r"\b(?:bleach|ammonia|chlorine|antifreeze|lye|caustic soda)\b",
        "Mogelijk giftige of bijtende chemische stof.",
    ),
    (
        "DANGEROUS",
        r"\b(?:belladonna|deadly nightshade|hemlock|aconite|wolfsbane|foxglove)\b",
        "Mogelijk giftige plant of stof.",
    ),
    (
        "DANGEROUS",
        r"\b(?:drink|swallow|ingest|eat)\b.{0,50}\b(?:essential oil|ashes?|blood|unknown herb)\b",
        "Mogelijk onveilige inname.",
    ),
    (
        "DANGEROUS",
        r"\b(?:cut yourself|self[- ]?harm|suicide|kill yourself|human sacrifice)\b",
        "Instructie kan ernstig lichamelijk letsel veroorzaken.",
    ),
    (
        "REVIEW",
        r"\b(?:open flame|bonfire|set (?:it|this|them) on fire|burn (?:it|this|paper|herbs?))\b",
        "Brand- of rookrisico; alleen na menselijke veiligheidscontrole.",
    ),
    (
        "REVIEW",
        r"\b(?:knife|blade|razor|needle|gun|weapon)\b",
        "Scherp voorwerp of wapen genoemd.",
    ),
    (
        "REVIEW",
        r"\b(?:animal sacrifice|harm (?:an|the) animal|live animal)\b",
        "Mogelijke schade aan dieren.",
    ),
    (
        "REVIEW",
        r"\b(?:make (?:him|her|them) love|against (?:his|her|their) will|without consent)\b",
        "Mogelijke handeling zonder toestemming van een ander.",
    ),
    (
        "REVIEW",
        r"\b(?:curse|hex|revenge|binding spell|bind (?:him|her|them)|baneful)\b",
        "Schadelijke of dwingende intentie; menselijke beoordeling vereist.",
    ),
    (
        "REVIEW",
        r"\b(?:graveyard|cemetery|human remains|bones? from a grave)\b",
        "Mogelijke juridische, ethische of gezondheidsrisico's.",
    ),
)


def assess_safety(text: str) -> SafetyAssessment:
    """Markeer risico's zonder te beweren dat een niet-gematchte tekst risicoloos is."""

    statuses: list[str] = []
    notes: list[str] = []
    consent_safeguard = bool(
        re.search(
            r"\b(?:free choice|with consent|consensual|right for both|if it is right for both)\b",
            text,
            flags=re.IGNORECASE,
        )
    )
    for status, pattern, note in _RULES:
        if re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL):
            if note.startswith("Schadelijke of dwingende") and consent_safeguard:
                harmful_without_binding = re.search(
                    r"\b(?:curse|hex|revenge|bind (?:him|her|them)|baneful)\b",
                    text,
                    flags=re.IGNORECASE,
                )
                if not harmful_without_binding:
                    continue
            statuses.append(status)
            if note not in notes:
                notes.append(note)

    if "DANGEROUS" in statuses:
        return SafetyAssessment("DANGEROUS", notes, False)
    if "REVIEW" in statuses:
        return SafetyAssessment("REVIEW", notes, False)
    return SafetyAssessment(
        "NO_RISK_DETECTED",
        ["Geen automatisch risicosignaal gevonden; dit is geen veiligheidsgarantie."],
        True,
    )
