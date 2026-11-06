"""Optionele lokale Ollama-analyse met bron-evidencevalidatie."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class OllamaUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class OllamaResult:
    spells: list[dict[str, Any]]
    model: str
    validation_errors: list[str]


RequestFunction = Callable[[urllib.request.Request, float], bytes]


def _default_request(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - localhost configurable
        return response.read()


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "spells": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "purpose": {"type": "string"},
                    "materials": {"type": "array", "items": {"type": "string"}},
                    "preparation": {"type": "array", "items": {"type": "string"}},
                    "instructions": {"type": "array", "items": {"type": "string"}},
                    "incantation": {"type": "string"},
                    "timing": {"type": "string"},
                    "closing_steps": {"type": "array", "items": {"type": "string"}},
                    "language": {"type": "string"},
                    "description_nl": {"type": "string"},
                    "status": {"enum": ["COMPLETE", "INCOMPLETE", "REVIEW"]},
                    "completeness_score": {"type": "number", "minimum": 0, "maximum": 100},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "required": [
                    "title",
                    "purpose",
                    "materials",
                    "preparation",
                    "instructions",
                    "incantation",
                    "timing",
                    "closing_steps",
                    "language",
                    "description_nl",
                    "status",
                    "completeness_score",
                    "confidence",
                    "evidence",
                ],
            },
        }
    },
    "required": ["spells"],
}


class OllamaAnalyzer:
    def __init__(
        self,
        model: str,
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 180,
        request_fn: RequestFunction | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._request = request_fn or _default_request

    def _call(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "magical-spell-collector/0.1",
            },
            method="POST" if data is not None else "GET",
        )
        try:
            raw = self._request(request, self.timeout)
            return json.loads(raw.decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise OllamaUnavailable(f"Ollama niet bereikbaar of gaf ongeldige JSON: {exc}") from exc

    def models(self) -> list[str]:
        payload = self._call("/api/tags")
        return [item["name"] for item in payload.get("models", []) if item.get("name")]

    def analyze(self, text: str) -> OllamaResult:
        prompt = (
            "Analyseer uitsluitend de bron tussen SOURCE-tags. Vind nul of meer afzonderlijke rituelen. "
            "Neem geen ingrediënt, stap, spreuktekst, timing of betekenis op die niet letterlijk in de bron staat. "
            "Een vraag of discussie is geen complete spreuk. COMPLETE vereist een doel of duidelijke omschrijving "
            "en een uitvoerbare handelingenreeks; materialen en gesproken woorden zijn alleen nodig wanneer dit "
            "type ritueel ze gebruikt. Geef per inhoudsveld korte, exacte evidence-fragmenten uit de bron. "
            "Een titel mag beschrijvend worden gegenereerd. description_nl moet expliciet als broninterpretatie "
            "zijn geformuleerd en mag magische werking niet als wetenschappelijk bewezen presenteren.\n"
            f"<SOURCE>\n{text}\n</SOURCE>"
        )
        payload = self._call(
            "/api/chat",
            {
                "model": self.model,
                "stream": False,
                "format": _SCHEMA,
                "options": {"temperature": 0},
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        content = payload.get("message", {}).get("content", "")
        try:
            parsed = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise OllamaUnavailable(
                f"Ollama-resultaat bevat geen geldige analyse-JSON: {exc}"
            ) from exc
        return self._validate(parsed, text)

    def _validate(self, parsed: dict[str, Any], source_text: str) -> OllamaResult:
        errors: list[str] = []
        accepted: list[dict[str, Any]] = []
        for index, spell in enumerate(parsed.get("spells", [])):
            if not isinstance(spell, dict):
                errors.append(f"spell[{index}] is geen object")
                continue
            evidence = spell.get("evidence", {})
            if not isinstance(evidence, dict):
                evidence = {}
            invalid_evidence = False
            for field, excerpts in evidence.items():
                if not isinstance(excerpts, list):
                    invalid_evidence = True
                    errors.append(f"spell[{index}].evidence.{field} is geen lijst")
                    continue
                for excerpt in excerpts:
                    if not isinstance(excerpt, str) or excerpt not in source_text:
                        invalid_evidence = True
                        errors.append(f"spell[{index}].{field} heeft niet-herleidbare evidence")

            for field in (
                "purpose",
                "materials",
                "preparation",
                "instructions",
                "incantation",
                "timing",
                "closing_steps",
            ):
                value = spell.get(field)
                values = value if isinstance(value, list) else [value] if value else []
                for item in values:
                    if isinstance(item, str) and item not in source_text:
                        invalid_evidence = True
                        errors.append(f"spell[{index}].{field} bevat niet-letterlijke broninhoud")

            required_fields = ("purpose", "instructions")
            for field in required_fields:
                value = spell.get(field)
                if value and not evidence.get(field):
                    invalid_evidence = True
                    errors.append(f"spell[{index}].{field} mist evidence")

            if invalid_evidence:
                spell["status"] = "REVIEW"
                spell["confidence"] = min(float(spell.get("confidence", 0)), 0.49)
            if spell.get("status") == "COMPLETE" and not spell.get("instructions"):
                spell["status"] = "INCOMPLETE"
                errors.append(f"spell[{index}] was COMPLETE zonder stappen")
            accepted.append(spell)
        return OllamaResult(accepted, self.model, errors)
