"""Rechtenbewuste JSON-, JSONL-, CSV- en Markdown-exports."""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spell_collector.config import AppConfig
from spell_collector.db import Database
from spell_collector.pipeline import directory_size

_EXPORT_FIELDS = (
    "unique_id",
    "title",
    "title_original",
    "language",
    "category",
    "categories",
    "subcategories",
    "tags",
    "purpose",
    "materials",
    "preparation",
    "instructions",
    "incantation",
    "timing",
    "closing_steps",
    "historical_tradition",
    "symbolism",
    "structured_interpretation_nl",
    "source_subreddit",
    "source_url",
    "source_post_id",
    "source_comment_ids",
    "source_date",
    "source_author",
    "original_text_reference",
    "completeness_status",
    "completeness_score",
    "extraction_confidence",
    "safety_status",
    "safety_notes",
    "rights_status",
    "review_status",
    "publication_status",
    "date_collected",
)


@dataclass(slots=True)
class ExportOutcome:
    complete_records: int
    incomplete_records: int
    review_records: int
    publication_ready_records: int
    files: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "complete_records": self.complete_records,
            "incomplete_records": self.incomplete_records,
            "review_records": self.review_records,
            "publication_ready_records": self.publication_ready_records,
            "files": self.files,
        }


def _atomic_write(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding=encoding, newline="")
    temporary.replace(path)


def _clean_record(spell: dict[str, Any]) -> dict[str, Any]:
    return {
        field: spell.get(
            field,
            []
            if field
            in {
                "categories",
                "subcategories",
                "tags",
                "materials",
                "preparation",
                "instructions",
                "closing_steps",
                "source_comment_ids",
                "safety_notes",
            }
            else "",
        )
        for field in _EXPORT_FIELDS
    }


def _json_text(records: list[dict[str, Any]]) -> str:
    return json.dumps(records, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def _jsonl_text(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in records)


def _csv_text(records: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(_EXPORT_FIELDS), extrasaction="ignore")
    writer.writeheader()
    list_fields = {
        "categories",
        "subcategories",
        "tags",
        "materials",
        "preparation",
        "instructions",
        "closing_steps",
        "source_comment_ids",
        "safety_notes",
    }
    for record in records:
        row = dict(record)
        for field in list_fields:
            row[field] = json.dumps(row.get(field, []), ensure_ascii=False)
        writer.writerow(row)
    return stream.getvalue()


def _slug(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.casefold()).strip("-") or "overig"


def _anchor(value: str) -> str:
    return _slug(value)


def _list_section(title: str, values: list[str], numbered: bool = False) -> str:
    if not values:
        return ""
    lines = [f"### {title}", ""]
    marker = (lambda index: f"{index}.") if numbered else (lambda _index: "-")
    lines.extend(f"{marker(index)} {value}" for index, value in enumerate(values, start=1))
    return "\n".join(lines) + "\n\n"


def _spell_markdown(spell: dict[str, Any]) -> str:
    lines = [f"## {spell['title']}", ""]
    lines.append(f"**Categorie:** {spell['category']}")
    lines.append("")
    if spell.get("purpose"):
        lines.extend((f"**Doel:** {spell['purpose']}", ""))
    if spell.get("historical_tradition"):
        lines.extend((f"**Traditie:** {spell['historical_tradition']}", ""))
    if spell.get("structured_interpretation_nl"):
        lines.extend(
            (f"**Nederlandse broninterpretatie:** {spell['structured_interpretation_nl']}", "")
        )
    body = "\n".join(lines) + "\n"
    body += _list_section("Benodigdheden", spell.get("materials", []))
    body += _list_section("Voorbereiding", spell.get("preparation", []), numbered=True)
    body += _list_section("Uitvoering", spell.get("instructions", []), numbered=True)
    if spell.get("incantation"):
        body += f"### Bezwering of gesproken tekst\n\n{spell['incantation']}\n\n"
    if spell.get("timing"):
        body += f"### Timing\n\n{spell['timing']}\n\n"
    body += _list_section("Afronding", spell.get("closing_steps", []), numbered=True)
    if spell.get("symbolism"):
        body += f"### Symboliek en achtergrond\n\n{spell['symbolism']}\n\n"
    body += "### Bron\n\n"
    body += f"- Subreddit: r/{spell['source_subreddit']}\n"
    body += f"- URL: {spell['source_url']}\n"
    body += f"- Bron-ID: {spell['source_post_id']}\n"
    if spell.get("source_comment_ids"):
        body += f"- Aanvullende reacties: {', '.join(spell['source_comment_ids'])}\n"
    body += f"- Rechtenstatus: {spell['rights_status']}\n"
    body += f"- Extractiebetrouwbaarheid: {spell['extraction_confidence']}\n\n"
    body += "> Bronbewering, geen wetenschappelijk bewezen magische werking. Publiceer niet zonder rechtencontrole.\n\n"
    return body


def _collection_markdown(records: list[dict[str, Any]], title: str) -> str:
    categories: dict[str, list[dict[str, Any]]] = {}
    for spell in records:
        categories.setdefault(spell["category"] or "Overige tradities", []).append(spell)
    lines = [
        f"# {title}",
        "",
        "Uitsluitend voor lokaal onderzoek. Bronteksten zijn niet opgenomen.",
        "",
        "## Inhoud",
        "",
    ]
    for category in sorted(categories):
        lines.append(f"- [{category}](#{_anchor(category)})")
        for spell in categories[category]:
            lines.append(f"  - [{spell['title']}](#{_anchor(spell['title'])})")
    lines.append("")
    for category in sorted(categories):
        lines.extend((f"# {category}", ""))
        for spell in categories[category]:
            lines.append(_spell_markdown(spell).rstrip())
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _publication_ready(spell: dict[str, Any]) -> bool:
    rights_ok = spell.get("rights_status") in {"PERMISSION_GRANTED", "PUBLIC_DOMAIN"}
    permission_ok = (
        bool(spell.get("publication_permission")) or spell.get("rights_status") == "PUBLIC_DOMAIN"
    )
    return bool(
        spell.get("completeness_status") == "COMPLETE"
        and spell.get("safety_status") == "SAFE"
        and not spell.get("removal_requested")
        and rights_ok
        and permission_ok
        and spell.get("review_status") == "APPROVED"
    )


def _report(
    database: Database,
    config: AppConfig,
    complete: list[dict[str, Any]],
    incomplete: list[dict[str, Any]],
    review: list[dict[str, Any]],
    public: list[dict[str, Any]],
) -> str:
    stats = database.statistics().as_dict()
    category_counts = Counter(item["category"] or "Overig" for item in complete)
    rights_counts = Counter(item["rights_status"] for item in database.list_spells())
    safety_counts = Counter(item["safety_status"] for item in database.list_spells())
    errors = database.unresolved_errors()
    lines = [
        "# Rapport Magical Spell Collector",
        "",
        f"Gegenereerd: {datetime.now(UTC).isoformat()}",
        "",
        "## Werkelijke resultaten",
        "",
        f"- Verzamelde berichten: {stats['posts']}",
        f"- Verzamelde reacties: {stats['comments']}",
        f"- Geanalyseerde bronrecords: {stats['analyzed_records']}",
        f"- Potentiële spreuken: {stats['potential_spells']}",
        f"- Complete, unieke en automatisch veilig bevonden records in hoofdexport: {len(complete)}",
        f"- Onvolledige records: {len(incomplete)}",
        f"- Records voor beoordeling: {len(review)}",
        f"- Duplicaten: {stats['duplicate_spells']}",
        f"- Publicatieklare records met rechtencontrole: {len(public)}",
        f"- Onopgeloste fouten: {len(errors)}",
        f"- Gebruikte projectopslag: {directory_size(config.storage.data_dir)} bytes",
        "",
        "## Complete records per categorie",
        "",
    ]
    lines.extend(f"- {name}: {count}" for name, count in sorted(category_counts.items()))
    if not category_counts:
        lines.append("- Nog geen complete records.")
    lines.extend(("", "## Rechtenstatus", ""))
    lines.extend(f"- {name}: {count}" for name, count in sorted(rights_counts.items()))
    lines.extend(("", "## Veiligheidsstatus", ""))
    lines.extend(f"- {name}: {count}" for name, count in sorted(safety_counts.items()))
    lines.extend(
        (
            "",
            "## Aandachtspunten voor publicatie",
            "",
            "- Reddit-auteurs behouden in beginsel hun auteursrechten; UNKNOWN is geen publicatietoestemming.",
            "- Volledige brontekst wordt niet geëxporteerd. Vraag toestemming en registreer die vóór publicatie.",
            "- Veiligheidsdetectie is conservatief maar geen vervanging voor deskundige menselijke beoordeling.",
            "- Magische effecten worden als bronbewering beschreven en zijn niet wetenschappelijk bewezen.",
            "- Verwijderingsverzoeken moeten ook na export worden toegepast.",
            "",
            "## Fouten",
            "",
        )
    )
    if errors:
        lines.extend(
            f"- [{item['stage']}] {item['error_type']}: {item['message']}" for item in errors[:100]
        )
    else:
        lines.append("- Geen onopgeloste fouten geregistreerd.")
    return "\n".join(lines).rstrip() + "\n"


def export_all(database: Database, config: AppConfig) -> ExportOutcome:
    output = config.storage.export_dir
    output.mkdir(parents=True, exist_ok=True)
    all_spells = database.list_spells()
    complete = [
        _clean_record(item)
        for item in all_spells
        if item["completeness_status"] == "COMPLETE"
        and item["safety_status"] == "SAFE"
        and not item["removal_requested"]
    ]
    incomplete = [
        _clean_record(item) for item in all_spells if item["completeness_status"] == "INCOMPLETE"
    ]
    review = [
        _clean_record(item)
        for item in all_spells
        if item["completeness_status"] == "REVIEW"
        or item["review_status"] in {"PENDING", "NEEDS_REVIEW"}
        or item["safety_status"] in {"FLAGGED", "EXCLUDED"}
    ]
    public_full = [item for item in all_spells if _publication_ready(item)]
    public = [_clean_record(item) for item in public_full]

    rendered: dict[Path, tuple[str, str]] = {
        output / "spells.json": (_json_text(complete), "utf-8"),
        output / "spells.jsonl": (_jsonl_text(complete), "utf-8"),
        output / "spells.csv": (_csv_text(complete), config.export.csv_encoding),
        output / "complete-spell-collection.md": (
            _collection_markdown(complete, "Complete Magical Spell Collection"),
            "utf-8",
        ),
        output / "incomplete-spells.jsonl": (_jsonl_text(incomplete), "utf-8"),
        output / "review-queue.jsonl": (_jsonl_text(review), "utf-8"),
        output / "publication-ready.json": (_json_text(public), "utf-8"),
        output / "publication-ready.md": (
            _collection_markdown(
                public, "Juridisch en inhoudelijk gecontroleerde publicatieselectie"
            ),
            "utf-8",
        ),
    }
    if config.export.markdown_per_category:
        by_category: dict[str, list[dict[str, Any]]] = {}
        for spell in complete:
            by_category.setdefault(spell["category"] or "Overige tradities", []).append(spell)
        for category, spells in by_category.items():
            rendered[output / "categories" / f"{_slug(category)}.md"] = (
                _collection_markdown(spells, category),
                "utf-8",
            )
    report = _report(database, config, complete, incomplete, review, public)
    rendered[output / "collection-report.md"] = (report, "utf-8")

    # Verwijder uitsluitend verouderde, door deze exporter gemaakte
    # categorie-Markdown binnen de expliciete exportmap.
    category_dir = (output / "categories").resolve()
    expected_category_files = {
        path.resolve() for path in rendered if path.parent.resolve() == category_dir
    }
    if category_dir.is_dir() and output.resolve() in category_dir.parents:
        for old_file in category_dir.glob("*.md"):
            if old_file.resolve() not in expected_category_files:
                old_file.unlink()

    files: list[dict[str, Any]] = []
    for path, (content, encoding) in rendered.items():
        _atomic_write(path, content, encoding=encoding)
        files.append({"path": str(path), "bytes": path.stat().st_size})
    return ExportOutcome(len(complete), len(incomplete), len(review), len(public), files)


def verify_exports(database: Database, config: AppConfig) -> dict[str, Any]:
    output = config.storage.export_dir
    required = (
        "spells.json",
        "spells.jsonl",
        "spells.csv",
        "complete-spell-collection.md",
        "collection-report.md",
        "publication-ready.json",
    )
    issues: list[str] = []
    for name in required:
        if not (output / name).is_file():
            issues.append(f"Ontbrekend exportbestand: {name}")
    if issues:
        return {"ok": False, "issues": issues, "checks": {"required_files": False}}

    try:
        spells = json.loads((output / "spells.json").read_text(encoding="utf-8"))
        publication = json.loads(
            (output / "publication-ready.json").read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as exc:
        issues.append(f"Ongeldige JSON: {exc}")
        spells, publication = [], []

    jsonl_records: list[dict[str, Any]] = []
    try:
        with (output / "spells.jsonl").open(encoding="utf-8") as handle:
            jsonl_records = [json.loads(line) for line in handle if line.strip()]
    except json.JSONDecodeError as exc:
        issues.append(f"Ongeldige JSONL: {exc}")

    try:
        with (output / "spells.csv").open(encoding="utf-8-sig", newline="") as handle:
            csv_records = list(csv.DictReader(handle))
    except (OSError, csv.Error) as exc:
        issues.append(f"Ongeldige CSV: {exc}")
        csv_records = []

    expected_complete = [
        item
        for item in database.list_spells()
        if item["completeness_status"] == "COMPLETE"
        and item["safety_status"] == "SAFE"
        and not item["removal_requested"]
    ]
    if not (len(spells) == len(jsonl_records) == len(csv_records) == len(expected_complete)):
        issues.append("Recordaantallen van database, JSON, JSONL en CSV verschillen")
    for index, spell in enumerate(spells):
        missing = [field for field in _EXPORT_FIELDS if field not in spell]
        if missing:
            issues.append(f"spells.json[{index}] mist velden: {', '.join(missing)}")
        if spell.get("completeness_status") != "COMPLETE":
            issues.append(f"spells.json[{index}] is niet COMPLETE")
        if spell.get("safety_status") != "SAFE":
            issues.append(f"spells.json[{index}] is niet SAFE")
        if not spell.get("instructions"):
            issues.append(f"spells.json[{index}] mist uitvoerbare stappen")
        if "original_text" in spell:
            issues.append(f"spells.json[{index}] bevat ongewenste volledige brontekst")
        if not str(spell.get("source_url", "")).startswith("https://www.reddit.com/"):
            issues.append(f"spells.json[{index}] heeft geen herleidbare Reddit-URL")
    for index, spell in enumerate(publication):
        if spell.get("rights_status") not in {"PERMISSION_GRANTED", "PUBLIC_DOMAIN"}:
            issues.append(f"publication-ready.json[{index}] mist goedgekeurde rechten")

    markdown = (output / "complete-spell-collection.md").read_text(encoding="utf-8")
    report = (output / "collection-report.md").read_text(encoding="utf-8")
    if not markdown.startswith("# "):
        issues.append("Hoofd-Markdown mist een titel")
    if "Werkelijke resultaten" not in report:
        issues.append("Collectierapport mist werkelijke resultaten")

    checks = {
        "required_files": True,
        "json_records": len(spells),
        "jsonl_records": len(jsonl_records),
        "csv_records": len(csv_records),
        "database_exportable_records": len(expected_complete),
        "publication_ready_records": len(publication),
        "markdown_utf8": True,
        "full_source_text_excluded": not any("original_text" in item for item in spells),
    }
    return {"ok": not issues, "issues": issues, "checks": checks}
