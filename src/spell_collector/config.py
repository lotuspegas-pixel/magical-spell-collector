"""Configuration loading for Magical Spell Collector.

Paths in YAML files are resolved relative to the project directory, never to
the caller's current working directory.  A user configuration is merged over
``config/default.yaml`` so new defaults remain backwards compatible.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGED_DEFAULT_CONFIG_PATH = Path(__file__).with_name("default.yaml")
REPOSITORY_DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"
DEFAULT_CONFIG_PATH = (
    REPOSITORY_DEFAULT_CONFIG_PATH
    if REPOSITORY_DEFAULT_CONFIG_PATH.is_file()
    else PACKAGED_DEFAULT_CONFIG_PATH
)


class ConfigError(ValueError):
    """Raised when a configuration file is absent or invalid."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Configuratiebestand bestaat niet: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Ongeldige YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"De hoofdstructuur van {path} moet een mapping zijn")
    return data


def _project_root_for(config_path: Path, explicit_root: Path | None) -> Path:
    if explicit_root is not None:
        return explicit_root.expanduser().resolve()
    parent = config_path.parent
    if parent.name.casefold() == "config":
        return parent.parent.resolve()
    return parent.resolve()


def _resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


@dataclass(slots=True, frozen=True)
class StorageConfig:
    data_dir: Path
    database: Path
    raw_dir: Path
    export_dir: Path
    log_dir: Path
    max_gb: float = 5.0
    reserve_free_gb: float = 2.0


@dataclass(slots=True, frozen=True)
class CollectionConfig:
    subreddits: tuple[str, ...]
    kinds: str = "both"
    pilot_subreddit: str = "Spells"
    pilot_max_records: int = 100
    batch_size: int = 100
    api_delay_seconds: float = 1.0
    prefer_local_archives: bool = True
    allow_api_fallback: bool = False


@dataclass(slots=True, frozen=True)
class AnalysisConfig:
    search_terms: tuple[str, ...]
    categories: dict[str, tuple[str, ...]]
    completeness_threshold: float = 0.72
    review_threshold: float = 0.50
    exact_duplicate_threshold: float = 0.98
    near_duplicate_threshold: float = 0.88
    language_hints: tuple[str, ...] = ("en", "nl")


@dataclass(slots=True, frozen=True)
class OllamaConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:11434"
    model: str = ""
    timeout_seconds: int = 120


@dataclass(slots=True, frozen=True)
class ExportConfig:
    include_original_text: bool = False
    require_rights_approval_for_full_text: bool = True
    csv_encoding: str = "utf-8-sig"
    markdown_per_category: bool = True


@dataclass(slots=True, frozen=True)
class SafetyConfig:
    exclude_flagged_from_publication: bool = True
    hazardous_terms: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class RightsConfig:
    default_status: str = "UNKNOWN"
    publication_requires_permission: bool = True
    retain_source_author: bool = True


@dataclass(slots=True, frozen=True)
class AppConfig:
    project_root: Path
    config_path: Path
    storage: StorageConfig
    collection: CollectionConfig
    analysis: AnalysisConfig
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    rights: RightsConfig = field(default_factory=RightsConfig)

    def ensure_directories(self) -> None:
        for path in (
            self.storage.data_dir,
            self.storage.raw_dir,
            self.storage.export_dir,
            self.storage.log_dir,
            self.storage.database.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _as_string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"'{name}' moet een lijst met tekstwaarden zijn")
    return tuple(item.strip() for item in value if item.strip())


def load_config(
    path: str | Path | None = None,
    *,
    project_root: str | Path | None = None,
) -> AppConfig:
    """Load defaults plus an optional override and resolve all local paths."""

    using_packaged_default = path is None and DEFAULT_CONFIG_PATH == PACKAGED_DEFAULT_CONFIG_PATH
    config_path = Path(path).expanduser().resolve() if path else DEFAULT_CONFIG_PATH.resolve()
    base = _read_yaml(PACKAGED_DEFAULT_CONFIG_PATH)
    override = {} if config_path == PACKAGED_DEFAULT_CONFIG_PATH.resolve() else _read_yaml(config_path)
    raw = _deep_merge(base, override)
    if project_root is None and using_packaged_default:
        root = Path.cwd().resolve()
    else:
        root = _project_root_for(
            config_path,
            Path(project_root) if project_root is not None else None,
        )

    try:
        storage_raw = raw["storage"]
        collection_raw = raw["collection"]
        analysis_raw = raw["analysis"]
        category_raw = analysis_raw["categories"]
        if not isinstance(category_raw, dict):
            raise ConfigError("'analysis.categories' moet een mapping zijn")

        storage = StorageConfig(
            data_dir=_resolve_path(root, storage_raw["data_dir"]),
            database=_resolve_path(root, storage_raw["database"]),
            raw_dir=_resolve_path(root, storage_raw["raw_dir"]),
            export_dir=_resolve_path(root, storage_raw["export_dir"]),
            log_dir=_resolve_path(root, storage_raw["log_dir"]),
            max_gb=float(storage_raw.get("max_gb", 5.0)),
            reserve_free_gb=float(storage_raw.get("reserve_free_gb", 2.0)),
        )
        collection = CollectionConfig(
            subreddits=_as_string_tuple(collection_raw["subreddits"], "collection.subreddits"),
            kinds=str(collection_raw.get("kinds", "both")),
            pilot_subreddit=str(collection_raw.get("pilot_subreddit", "Spells")),
            pilot_max_records=int(collection_raw.get("pilot_max_records", 100)),
            batch_size=int(collection_raw.get("batch_size", 100)),
            api_delay_seconds=float(collection_raw.get("api_delay_seconds", 1.0)),
            prefer_local_archives=bool(collection_raw.get("prefer_local_archives", True)),
            allow_api_fallback=bool(collection_raw.get("allow_api_fallback", False)),
        )
        categories = {
            str(name): _as_string_tuple(values, f"analysis.categories.{name}")
            for name, values in category_raw.items()
        }
        analysis = AnalysisConfig(
            search_terms=_as_string_tuple(analysis_raw["search_terms"], "analysis.search_terms"),
            categories=categories,
            completeness_threshold=float(analysis_raw.get("completeness_threshold", 0.72)),
            review_threshold=float(analysis_raw.get("review_threshold", 0.50)),
            exact_duplicate_threshold=float(analysis_raw.get("exact_duplicate_threshold", 0.98)),
            near_duplicate_threshold=float(analysis_raw.get("near_duplicate_threshold", 0.88)),
            language_hints=_as_string_tuple(
                analysis_raw.get("language_hints", ["en", "nl"]),
                "analysis.language_hints",
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"Ontbrekende of ongeldige configuratiewaarde: {exc}") from exc

    if not collection.subreddits:
        raise ConfigError("Configureer minimaal één subreddit")
    if not 1 <= collection.pilot_max_records <= 100:
        raise ConfigError("collection.pilot_max_records moet tussen 1 en 100 liggen")
    if collection.batch_size < 1:
        raise ConfigError("collection.batch_size moet minimaal 1 zijn")
    if storage.max_gb <= 0 or storage.reserve_free_gb < 0:
        raise ConfigError("Opslaggrenzen moeten positief zijn")
    if not 0 <= analysis.review_threshold <= analysis.completeness_threshold <= 1:
        raise ConfigError("Analyse-drempels moeten oplopend tussen 0 en 1 liggen")

    ollama_raw = raw.get("ollama", {})
    export_raw = raw.get("export", {})
    safety_raw = raw.get("safety", {})
    rights_raw = raw.get("rights", {})
    return AppConfig(
        project_root=root,
        config_path=config_path,
        storage=storage,
        collection=collection,
        analysis=analysis,
        ollama=OllamaConfig(
            enabled=bool(ollama_raw.get("enabled", False)),
            base_url=str(ollama_raw.get("base_url", "http://127.0.0.1:11434")).rstrip("/"),
            model=str(ollama_raw.get("model", "")),
            timeout_seconds=int(ollama_raw.get("timeout_seconds", 120)),
        ),
        export=ExportConfig(
            include_original_text=bool(export_raw.get("include_original_text", False)),
            require_rights_approval_for_full_text=bool(
                export_raw.get("require_rights_approval_for_full_text", True)
            ),
            csv_encoding=str(export_raw.get("csv_encoding", "utf-8-sig")),
            markdown_per_category=bool(export_raw.get("markdown_per_category", True)),
        ),
        safety=SafetyConfig(
            exclude_flagged_from_publication=bool(
                safety_raw.get("exclude_flagged_from_publication", True)
            ),
            hazardous_terms=_as_string_tuple(
                safety_raw.get("hazardous_terms", []), "safety.hazardous_terms"
            ),
        ),
        rights=RightsConfig(
            default_status=str(rights_raw.get("default_status", "UNKNOWN")).upper(),
            publication_requires_permission=bool(
                rights_raw.get("publication_requires_permission", True)
            ),
            retain_source_author=bool(rights_raw.get("retain_source_author", True)),
        ),
    )
