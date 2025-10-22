#!/usr/bin/env python3
"""企业级 schema 迁移流水线原型。

该脚本结合 `pipeline.config.yaml` 中的配置执行以下步骤：
1. 扫描早期 JSON schema 目录，建立已有域分类的基线。
2. 解析官方 YAML（或其他来源）的 `components.schemas` 内容。
3. 基于出现频次、命名映射与 API -> 域 的映射，自动推断模型归属域。
4. 重写 `$ref` 引用，输出符合企业目录结构的 JSON Schema 文件。

脚本参考了用户提供的本地处理脚本，主要保留其“分析/映射 -> 生成/重写”两阶段思路，
并加入配置化、日志提示与容错改进，便于后续在 CI/CD 流水线中复用。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, Mapping, MutableMapping, Optional, Sequence, Tuple

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - 仅在缺失依赖时执行
    yaml = None  # type: ignore[assignment]


@dataclass
class SpecDocument:
    """封装单个规范文件的解析结果。"""

    api_code: str
    version: Optional[str]
    major_version: Optional[str]
    protocol: str
    file: Path
    schemas: Mapping[str, object]


@dataclass(frozen=True)
class SchemaPlacement:
    """Schema 在输出目录中的放置策略。"""

    domain: str
    api_code: Optional[str] = None
    extra_segments: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelLocation:
    """描述某个模型在输出仓库中的相对路径信息。"""

    domain: str
    path: PurePosixPath
    api_code: Optional[str] = None
    api_name: Optional[str] = None
    extra_segments: Tuple[str, ...] = ()

    def filesystem_dir(self, root: Path) -> Path:
        return root / Path(self.path.as_posix())


@dataclass
class SchemaTaxonomy:
    """封装基于 YAML 的 Schema 分级信息。"""

    by_alias: Dict[str, SchemaPlacement]
    by_canonical: Dict[str, SchemaPlacement]

    def find(self, schema_name: str) -> Optional[SchemaPlacement]:
        placement = self.by_alias.get(schema_name)
        if placement is not None:
            return placement
        return self.by_canonical.get(canonical_model_name(schema_name))

    def canonical_items(self) -> Iterable[Tuple[str, SchemaPlacement]]:
        return self.by_canonical.items()


@dataclass(frozen=True)
class LocaleConfig:
    """多语言翻译的配置项。"""

    code: str
    directory: Path
    use_as_default: bool = False

    @property
    def output_code(self) -> str:
        return self.code


@dataclass
class OrganizedSchemaRecord:
    """记录用于目录重组的 Schema 元数据。"""

    model_name: str
    source_path: Path
    container: PurePosixPath
    domain: str
    api_code: Optional[str]
    api_name: Optional[str]
    version: Optional[str]
    major_version: Optional[str]
    protocol: str


@dataclass
class ModuleAccumulator:
    """聚合单个 API 模块的分类统计。"""

    domain: str
    api_code: Optional[str]
    api_name: Optional[str]
    module_parts: Tuple[str, ...]
    versions: Dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    source_versions: Dict[str, set] = field(default_factory=lambda: defaultdict(set))
    category_totals: Counter = field(default_factory=Counter)


@dataclass
class PipelineConfig:
    """结构化后的配置对象。"""

    tmf_official: Path
    external: Path
    common_candidate_threshold: int
    filename_pattern: re.Pattern[str]
    prefer_existing_domains: bool
    default_domain: str
    allowed_major_versions: Tuple[str, ...]
    output_root: Path
    output_docs: Path
    output_yaml: Path
    organized_root: Path
    domain_mapping_file: Path
    api_name_mapping_file: Path
    schema_taxonomy_file: Path
    schema_draft: str
    i18n_locales: Tuple[LocaleConfig, ...] = ()
    i18n_default_locale: str = "en"
    i18n_output_key: str = "x-i18n"
    validation_root: Optional[Path] = None
    validation_strip_keys: Tuple[str, ...] = ("x-metadata", "x-i18n")

    @classmethod
    def load(cls, config_path: Path) -> "PipelineConfig":
        if not config_path.exists():
            raise FileNotFoundError(f"未找到配置文件: {config_path}")
        _require_yaml(f"解析配置文件 {config_path}")
        with config_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        sources = raw.get("sources", {})
        processing = raw.get("processing", {})
        output = raw.get("output", {})
        validation = output.get("validation", {}) if isinstance(output, Mapping) else {}
        metadata = raw.get("metadata", {})
        i18n = raw.get("i18n", {})

        locales: list[LocaleConfig] = []
        for entry in i18n.get("locales", []) or []:
            if not isinstance(entry, Mapping):
                continue
            code = str(entry.get("code") or "").strip()
            path_value = entry.get("path") or entry.get("directory")
            if not code or not path_value:
                continue
            locales.append(
                LocaleConfig(
                    code=code,
                    directory=Path(path_value),
                    use_as_default=bool(entry.get("use_as_default", False)),
                )
            )

        default_locale = i18n.get("default_locale")
        i18n_default_locale = (
            str(default_locale).strip() if default_locale is not None else "en"
        )
        output_key = str(i18n.get("output_key", "x-i18n") or "x-i18n")

        strip_keys = tuple(
            str(key)
            for key in validation.get("strip_extensions", ["x-metadata", "x-i18n", "x-*"])
            if str(key)
        ) or ("x-metadata", "x-i18n", "x-*")

        validation_root_value = validation.get("root") if isinstance(validation, Mapping) else None
        validation_root = (
            Path(validation_root_value)
            if isinstance(validation_root_value, (str, Path)) and validation_root_value
            else None
        )

        allowed_versions = tuple(
            str(item).strip().lower()
            for item in processing.get("allowed_major_versions", [])
            if str(item).strip()
        )

        return cls(
            tmf_official=Path(sources.get("tmf_official", "sources/tmf-official")),
            external=Path(sources.get("external", "sources/external")),
            common_candidate_threshold=int(processing.get("common_candidate_threshold", 12)),
            filename_pattern=re.compile(processing.get("filename_regex", r"^(TMF\\d+)")),
            prefer_existing_domains=bool(processing.get("prefer_existing_domains", True)),
            default_domain=str(processing.get("default_domain", "Unclassified")),
            allowed_major_versions=allowed_versions,
    output_root=Path(output.get("root", "dist/json")),
    output_docs=Path(output.get("docs", "dist/docs")),
    output_yaml=Path(output.get("yaml", "dist/yaml")),
    organized_root=Path(output.get("organized", "dist/organized")),
            domain_mapping_file=Path(metadata.get("domain_mapping_file", "config/domain_mapping.yaml")),
            api_name_mapping_file=Path(
                metadata.get("api_name_mapping_file", "config/apiname_mapping.yaml")
            ),
            schema_taxonomy_file=Path(
                metadata.get("schema_taxonomy_file", "config/schema_taxonomy.yaml")
            ),
            schema_draft=str(metadata.get("schema_draft", "http://json-schema.org/draft-07/schema#")),
            i18n_locales=tuple(locales),
            i18n_default_locale=i18n_default_locale,
            i18n_output_key=output_key,
            validation_root=validation_root,
            validation_strip_keys=strip_keys,
        )


def _require_yaml(context: str) -> None:
    if yaml is None:
        raise SystemExit(
            f"{context} 需要 PyYAML 支持，请先执行 `pip install pyyaml` 或在隔离环境中预装该依赖。"
        )


if yaml is not None:
    YAML_EXCEPTIONS: Tuple[type[BaseException], ...] = (yaml.YAMLError,)
else:  # pragma: no cover - 仅用于缺失 PyYAML 的环境
    YAML_EXCEPTIONS = ()
JSON_AND_YAML_EXCEPTIONS = (json.JSONDecodeError,) + YAML_EXCEPTIONS


def load_domain_mapping(path: Path) -> Mapping[str, str]:
    if not path.exists():
        return {}
    _require_yaml(f"读取域映射文件 {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return {str(k): str(v) for k, v in data.items()}


def load_api_name_mapping(path: Path) -> Mapping[str, str]:
    if not path.exists():
        return {}
    _require_yaml(f"读取 API 名称映射 {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return {str(k).upper(): str(v) for k, v in data.items() if str(k)}


def _parse_schema_taxonomy_value(value: object) -> Optional[SchemaPlacement]:
    if value is None:
        return None
    if isinstance(value, Mapping):
        domain_value = value.get("domain")
        api_code_value = value.get("api") or value.get("api_code")
        extra_value = value.get("subfolders") or value.get("extra")
        domain = str(domain_value).strip() if domain_value else ""
        api_code = str(api_code_value).strip().upper() if api_code_value else None
        extra: Tuple[str, ...] = ()
        if isinstance(extra_value, (list, tuple)):
            extra = tuple(str(item).strip() for item in extra_value if str(item).strip())
        return SchemaPlacement(domain=domain, api_code=api_code or None, extra_segments=extra)

    text = str(value).strip()
    if not text:
        return None
    segments = [segment.strip() for segment in text.split("/") if segment.strip()]
    if not segments:
        return None
    api_code: Optional[str] = None
    domain: str = ""
    extra_segments: list[str] = []

    tmf_pattern = re.compile(r"^TMF\d+", re.IGNORECASE)
    if len(segments) >= 2 and tmf_pattern.match(segments[0]):
        api_code = segments[0].upper()
        domain = segments[1]
        if len(segments) > 2:
            extra_segments = segments[2:]
    else:
        domain = segments[0]
        if len(segments) > 1:
            extra_segments = segments[1:]

    if not domain:
        return None
    return SchemaPlacement(domain=domain, api_code=api_code, extra_segments=tuple(extra_segments))


def load_schema_taxonomy(path: Path) -> SchemaTaxonomy:
    alias_map: Dict[str, SchemaPlacement] = {}
    canonical_map: Dict[str, SchemaPlacement] = {}

    if not path.exists():
        return SchemaTaxonomy(by_alias=alias_map, by_canonical=canonical_map)

    _require_yaml(f"读取 Schema 分级文件 {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    if isinstance(data, Mapping):
        for raw_key, raw_value in data.items():
            key = str(raw_key).strip()
            if not key:
                continue
            placement = _parse_schema_taxonomy_value(raw_value)
            if placement is None:
                continue
            canonical_key = canonical_model_name(key)
            canonical_map[canonical_key] = placement
            for alias in iter_model_aliases(key):
                alias_map[alias] = placement

    return SchemaTaxonomy(by_alias=alias_map, by_canonical=canonical_map)


def collect_spec_sources(paths: Iterable[Path]) -> Iterable[Path]:
    suffixes = {".yaml", ".yml", ".json"}
    for base in paths:
        if not base.exists():
            continue
        for file in base.rglob("*"):
            if file.is_file() and file.suffix.lower() in suffixes:
                yield file


def load_spec_file(path: Path) -> Optional[Mapping[str, object]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        loader = json.load
    else:
        _require_yaml(f"解析规范文件 {path}")
        loader = yaml.safe_load
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = loader(fh) or {}
    except JSON_AND_YAML_EXCEPTIONS as exc:
        print(f"⚠️  解析失败 {path}: {exc}")
        return None
    return data if isinstance(data, Mapping) else None


def load_translation_file(path: Path) -> Optional[Mapping[str, object]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        loader = json.load
    else:
        _require_yaml(f"解析翻译文件 {path}")
        loader = yaml.safe_load
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = loader(fh) or {}
    except JSON_AND_YAML_EXCEPTIONS as exc:
        print(f"⚠️  翻译文件解析失败 {path}: {exc}")
        return None
    return data if isinstance(data, Mapping) else None


def detect_protocol(path: Path) -> str:
    name = path.name.lower()
    if "asyncapi" in name:
        return "asyncapi"
    if "openapi" in name or "oas" in name:
        return "openapi"
    for parent in path.parts:
        lower = parent.lower()
        if lower in {"asyncapi", "openapi"}:
            return lower
    return "unknown"


PROTOCOL_PRIORITY: Mapping[str, int] = {
    "openapi": 0,
    "asyncapi": 1,
    "unknown": 5,
}


def _sanitize_path_segment(value: str) -> str:
    sanitized = re.sub(r"[\\/:*?\"<>|]+", "_", value.strip())
    sanitized = re.sub(r"\s+", "_", sanitized)
    sanitized = sanitized.strip("_.")
    return sanitized or "segment"


def _normalize_api_code(api_code: Optional[str]) -> Optional[str]:
    if not api_code:
        return None
    text = str(api_code).strip().upper()
    return text or None


def build_model_location(
    domain: str,
    api_code: Optional[str],
    api_name_map: Mapping[str, str],
    extra_segments: Sequence[str],
    default_domain: str,
) -> ModelLocation:
    domain_label = domain.strip() if domain else ""
    if not domain_label:
        domain_label = default_domain

    path = PurePosixPath(_sanitize_path_segment(domain_label))
    sanitized_extra: list[str] = []
    for segment in extra_segments:
        clean_segment = _sanitize_path_segment(segment)
        if not clean_segment:
            continue
        path /= clean_segment
        sanitized_extra.append(clean_segment)

    normalized_code = _normalize_api_code(api_code)
    api_name: Optional[str] = None
    if normalized_code:
        api_name = api_name_map.get(normalized_code)
        folder_label = normalized_code if not api_name else f"{normalized_code}_{api_name}"
        path /= _sanitize_path_segment(folder_label)

    return ModelLocation(
        domain=domain_label,
        path=path,
        api_code=normalized_code,
        api_name=api_name,
        extra_segments=tuple(sanitized_extra),
    )


def determine_model_location(
    schema_name: str,
    canonical_name: str,
    document: SpecDocument,
    taxonomy: SchemaTaxonomy,
    domain_mapping: Mapping[str, str],
    schema_counts: Mapping[str, int],
    config: PipelineConfig,
    api_name_map: Mapping[str, str],
    cache: MutableMapping[str, ModelLocation],
) -> ModelLocation:
    existing = cache.get(canonical_name)
    if existing is not None:
        return existing

    placement = taxonomy.find(schema_name)
    if placement is None and canonical_name != schema_name:
        placement = taxonomy.find(canonical_name)

    if placement is not None:
        domain = placement.domain or config.default_domain
        api_code = placement.api_code if placement.api_code else document.api_code
        location = build_model_location(
            domain,
            api_code,
            api_name_map,
            placement.extra_segments,
            config.default_domain,
        )
    else:
        domain = infer_domain(
            canonical_name,
            schema_counts,
            domain_mapping,
            document.api_code,
            config,
        )
        location = build_model_location(
            domain,
            document.api_code,
            api_name_map,
            (),
            config.default_domain,
        )

    cache[canonical_name] = location
    return location


def _relative_ref_prefix(current: ModelLocation, target: ModelLocation) -> str:
    if current.path == target.path:
        return ""
    current_str = current.path.as_posix()
    target_str = target.path.as_posix()
    relative = os.path.relpath(target_str, current_str)
    if relative in {".", "./"}:
        return ""
    normalized = relative.replace("\\", "/")
    if not normalized.endswith("/"):
        normalized += "/"
    return normalized


def canonical_model_name(schema_name: str) -> str:
    """返回模型的规范名称（去除下划线后的后缀）。"""

    if "_" not in schema_name:
        return schema_name
    base, _, _ = schema_name.partition("_")
    return base or schema_name


def iter_model_aliases(schema_name: str) -> Iterable[str]:
    """针对翻译与域推断返回模型名的等价写法。"""

    canonical = canonical_model_name(schema_name)
    if canonical == schema_name:
        yield schema_name
    else:
        yield schema_name
        yield canonical


TRANSLATABLE_FIELDS = {"title", "description", "summary"}
ARRAY_TRANSLATABLE_KEYS = {"allOf", "anyOf", "oneOf"}
GLOBAL_PROPERTY_TRANSLATION_KEY = "__GLOBAL_PROPERTIES__"


def _merge_translation_payload(
    target: MutableMapping[str, object], updates: Mapping[str, object]
) -> None:
    for key, value in updates.items():
        if isinstance(value, Mapping):
            existing = target.get(key)
            if isinstance(existing, MutableMapping):
                _merge_translation_payload(existing, value)
            elif isinstance(existing, Mapping):
                nested: MutableMapping[str, object] = dict(existing)
                target[key] = nested
                _merge_translation_payload(nested, value)
            else:
                target[key] = dict(value)
        else:
            target[key] = value


def _sanitize_header(header: Optional[str]) -> str:
    if not header:
        return ""
    return header.strip().lstrip("\ufeff")


def _select_column(fieldnames: Iterable[str], candidates: Iterable[str]) -> Optional[str]:
    processed = [(_sanitize_header(name), _sanitize_header(name).lower()) for name in fieldnames]
    for candidate in candidates:
        candidate_clean = candidate.strip()
        if not candidate_clean:
            continue
        candidate_lower = candidate_clean.lower()
        contains_non_ascii = any(ord(ch) > 127 for ch in candidate_clean)
        for original, lowered in processed:
            collapsed = lowered.replace(" ", "")
            original_collapsed = original.replace(" ", "")
            if contains_non_ascii:
                if candidate_clean.replace(" ", "") in original_collapsed:
                    return original
            else:
                if candidate_lower in lowered or candidate_lower in collapsed:
                    return original
    return None


def _load_schema_level_csv(path: Path) -> Mapping[str, Mapping[str, object]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return {}
        reader.fieldnames = [_sanitize_header(name) for name in reader.fieldnames]

        schema_column = _select_column(reader.fieldnames, ["schema", "model", "name"])
        title_column = _select_column(
            reader.fieldnames,
            ["中文名称", "中文名", "名称", "title", "title zh"],
        )
        description_column = _select_column(
            reader.fieldnames,
            ["新描述", "中文描述", "描述", "说明", "description zh", "desc zh"],
        )

        if not schema_column or (not title_column and not description_column):
            return {}

        translations: Dict[str, Dict[str, object]] = {}
        for raw_row in reader:
            row = {(_sanitize_header(k)): (str(v).strip() if v is not None else "") for k, v in raw_row.items() if k}
            schema_name = row.get(schema_column, "").strip()
            if not schema_name:
                continue

            payload: Dict[str, object] = translations.setdefault(schema_name, {})
            title_value = row.get(title_column, "").strip() if title_column else ""
            description_value = row.get(description_column, "").strip() if description_column else ""

            if title_value:
                payload["title"] = title_value
            if description_value:
                payload["description"] = description_value

        return translations


def _load_property_level_csv(path: Path) -> Mapping[str, Mapping[str, object]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return {}
        reader.fieldnames = [_sanitize_header(name) for name in reader.fieldnames]

        schema_column = _select_column(reader.fieldnames, ["schema", "model", "name"])
        base_property_candidates = ["property", "field", "attribute", "属性", "字段"]
        property_column = _select_column(reader.fieldnames, base_property_candidates)
        if not property_column and "property" in path.stem.lower():
            property_column = _select_column(reader.fieldnames, ["descriptions", "属性描述"])
        title_column = _select_column(
            reader.fieldnames,
            ["中文名称", "中文名", "名称", "title", "title zh"],
        )
        description_column = _select_column(
            reader.fieldnames,
            ["新描述", "中文描述", "描述", "说明", "description zh", "desc zh"],
        )

        if not property_column and not description_column and not title_column:
            return {}

        translations: Dict[str, Dict[str, object]] = {}
        for raw_row in reader:
            row = {(_sanitize_header(k)): (str(v).strip() if v is not None else "") for k, v in raw_row.items() if k}
            schema_raw = row.get(schema_column, "").strip() if schema_column else ""
            property_raw = row.get(property_column, "").strip() if property_column else ""

            schema_name = schema_raw
            property_name = property_raw

            if not schema_name:
                schema_name = GLOBAL_PROPERTY_TRANSLATION_KEY

            if not property_name and schema_column and "." in schema_raw:
                schema_name, _, prop = schema_raw.partition(".")
                schema_name = schema_name.strip() or GLOBAL_PROPERTY_TRANSLATION_KEY
                property_name = prop.strip()

            if not property_name and not schema_column and "." in property_raw:
                schema_name, _, prop = property_raw.partition(".")
                schema_name = schema_name.strip() or GLOBAL_PROPERTY_TRANSLATION_KEY
                property_name = prop.strip()

            if not property_name:
                continue

            title_value = row.get(title_column, "").strip() if title_column else ""
            description_value = row.get(description_column, "").strip() if description_column else ""

            if not title_value and not description_value:
                continue

            schema_payload: Dict[str, object] = translations.setdefault(schema_name, {})
            existing_properties = schema_payload.setdefault("properties", {})
            if isinstance(existing_properties, MutableMapping):
                properties_container: MutableMapping[str, object] = existing_properties
            elif isinstance(existing_properties, Mapping):
                properties_container = dict(existing_properties)
                schema_payload["properties"] = properties_container
            else:
                properties_container = {}
                schema_payload["properties"] = properties_container

            prop_payload_raw = properties_container.get(property_name)
            if isinstance(prop_payload_raw, MutableMapping):
                prop_payload = prop_payload_raw
            elif isinstance(prop_payload_raw, Mapping):
                prop_payload = dict(prop_payload_raw)
                properties_container[property_name] = prop_payload
            else:
                prop_payload = {}
                properties_container[property_name] = prop_payload

            if title_value:
                prop_payload["title"] = title_value
            if description_value:
                prop_payload["description"] = description_value

        return translations


def load_locale_translations(locale: LocaleConfig) -> Mapping[str, Mapping[str, object]]:
    if not locale.directory.exists():
        return {}

    translations: Dict[str, Dict[str, object]] = {}
    for file in locale.directory.rglob("*"):
        if not file.is_file():
            continue

        suffix = file.suffix.lower()
        payloads: Mapping[str, Mapping[str, object]] = {}
        if suffix in {".yaml", ".yml", ".json"}:
            payload = load_translation_file(file)
            if not payload:
                continue

            schema_name = payload.get("schema") or payload.get("model") or payload.get("name")
            if isinstance(schema_name, str) and schema_name.strip():
                key = schema_name.strip()
            else:
                key = file.stem

            if "translations" in payload and isinstance(payload["translations"], Mapping):
                payloads = {key: payload["translations"]}  # type: ignore[assignment]
            else:
                filtered = {
                    k: v
                    for k, v in payload.items()
                    if k not in {"schema", "model", "name"}
                }
                if filtered:
                    payloads = {key: filtered}
                else:
                    payloads = {}
        elif suffix == ".csv":
            loader = (
                _load_property_level_csv
                if "property" in file.stem.lower()
                else _load_schema_level_csv
            )
            payloads = loader(file)
        else:
            continue

        for schema_key, schema_payload in payloads.items():
            if not schema_payload:
                continue
            existing = translations.get(schema_key)
            if isinstance(existing, MutableMapping):
                _merge_translation_payload(existing, schema_payload)
            elif isinstance(existing, Mapping):
                merged: MutableMapping[str, object] = dict(existing)
                _merge_translation_payload(merged, schema_payload)
                translations[schema_key] = merged
            else:
                translations[schema_key] = dict(schema_payload)

    return translations


def load_all_translations(
    locales: Tuple[LocaleConfig, ...]
) -> Tuple[Tuple[LocaleConfig, Mapping[str, Mapping[str, object]]], ...]:
    return tuple((locale, load_locale_translations(locale)) for locale in locales)


def _ensure_i18n_container(target: MutableMapping[str, object], output_key: str) -> MutableMapping[str, object]:
    existing = target.get(output_key)
    if isinstance(existing, MutableMapping):
        return existing
    if isinstance(existing, Mapping):
        container_map: MutableMapping[str, object] = dict(existing)
        target[output_key] = container_map
        return container_map
    container_map = {}
    target[output_key] = container_map
    return container_map


def _normalize_translation_value(
    value: object, locale: LocaleConfig
) -> Mapping[str, str]:
    if isinstance(value, str):
        return {locale.output_code: value}
    if isinstance(value, Mapping):
        normalized: Dict[str, str] = {}
        for key, text in value.items():
            if isinstance(key, str) and isinstance(text, str):
                normalized[key] = text
        return normalized
    return {}


def _apply_field_translation(
    target: MutableMapping[str, object],
    field: str,
    values: Mapping[str, str],
    locale: LocaleConfig,
    config: PipelineConfig,
) -> None:
    if not values:
        return
    i18n_container = _ensure_i18n_container(target, config.i18n_output_key)
    field_container_raw = i18n_container.get(field)
    if isinstance(field_container_raw, MutableMapping):
        field_container = field_container_raw
    elif isinstance(field_container_raw, Mapping):
        field_container = dict(field_container_raw)
        i18n_container[field] = field_container
    else:
        field_container = {}
        i18n_container[field] = field_container

    default_locale = config.i18n_default_locale
    default_value = target.get(field)
    if (
        default_locale
        and isinstance(default_value, str)
        and default_locale not in field_container
    ):
        field_container[default_locale] = default_value

    for lang_code, text in values.items():
        if isinstance(text, str):
            field_container[lang_code] = text

    if locale.use_as_default:
        preferred = values.get(locale.output_code)
        if preferred:
            target[field] = preferred


def _apply_translation_recursive(
    target: MutableMapping[str, object],
    translation: Mapping[str, object],
    locale: LocaleConfig,
    config: PipelineConfig,
) -> None:
    for key, value in translation.items():
        if key in TRANSLATABLE_FIELDS:
            normalized = _normalize_translation_value(value, locale)
            if key not in target and locale.use_as_default:
                target[key] = normalized.get(locale.output_code, target.get(key, ""))
            if key in target or normalized:
                _apply_field_translation(target, key, normalized, locale, config)
            continue

        if key == "properties" and isinstance(value, Mapping):
            properties = target.get("properties")
            if isinstance(properties, MutableMapping):
                for prop_name, prop_translation in value.items():
                    if not isinstance(prop_translation, Mapping):
                        continue
                    prop_target_raw = properties.get(prop_name)
                    if isinstance(prop_target_raw, MutableMapping):
                        _apply_translation_recursive(
                            prop_target_raw, prop_translation, locale, config
                        )
                    elif isinstance(prop_target_raw, Mapping):
                        nested = dict(prop_target_raw)
                        properties[prop_name] = nested
                        _apply_translation_recursive(
                            nested, prop_translation, locale, config
                        )
            continue

        if key == "definitions" and isinstance(value, Mapping):
            definitions = target.get("definitions")
            if isinstance(definitions, MutableMapping):
                for def_name, def_translation in value.items():
                    if not isinstance(def_translation, Mapping):
                        continue
                    def_target_raw = definitions.get(def_name)
                    if isinstance(def_target_raw, MutableMapping):
                        _apply_translation_recursive(
                            def_target_raw, def_translation, locale, config
                        )
            continue

        if key == "items" and isinstance(value, Mapping):
            items_target = target.get("items")
            if isinstance(items_target, MutableMapping):
                _apply_translation_recursive(items_target, value, locale, config)
            elif isinstance(items_target, Mapping):
                nested_items = dict(items_target)
                target["items"] = nested_items
                _apply_translation_recursive(nested_items, value, locale, config)
            continue

        if key in ARRAY_TRANSLATABLE_KEYS and isinstance(value, list):
            target_array = target.get(key)
            if isinstance(target_array, list):
                for idx, sub_translation in enumerate(value):
                    if not isinstance(sub_translation, Mapping):
                        continue
                    if idx >= len(target_array):
                        break
                    array_target_raw = target_array[idx]
                    if isinstance(array_target_raw, MutableMapping):
                        _apply_translation_recursive(
                            array_target_raw, sub_translation, locale, config
                        )
                    elif isinstance(array_target_raw, Mapping):
                        nested = dict(array_target_raw)
                        target_array[idx] = nested
                        _apply_translation_recursive(
                            nested, sub_translation, locale, config
                        )
            continue

        if isinstance(value, Mapping):
            nested_target_raw = target.get(key)
            if isinstance(nested_target_raw, MutableMapping):
                _apply_translation_recursive(
                    nested_target_raw, value, locale, config
                )
            elif isinstance(nested_target_raw, Mapping):
                nested_target = dict(nested_target_raw)
                target[key] = nested_target
                _apply_translation_recursive(nested_target, value, locale, config)


def _split_property_translations(
    payload: Mapping[str, object]
) -> Tuple[Mapping[str, object], Mapping[str, Mapping[str, object]]]:
    if not isinstance(payload, Mapping):
        return payload, {}

    residual: Dict[str, object] = {}
    properties_payload: Dict[str, Mapping[str, object]] = {}

    for key, value in payload.items():
        if key == "properties" and isinstance(value, Mapping):
            for prop_name, prop_translation in value.items():
                if isinstance(prop_translation, Mapping):
                    properties_payload[prop_name] = prop_translation
        else:
            residual[key] = value

    return residual, properties_payload


def _ensure_mutable_mapping(value: object) -> Optional[MutableMapping[str, object]]:
    if isinstance(value, MutableMapping):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    return None


def apply_property_translations_to_schema(
    schema: MutableMapping[str, object],
    properties_payload: Mapping[str, Mapping[str, object]],
    locale: LocaleConfig,
    config: PipelineConfig,
) -> None:
    if not properties_payload:
        return

    stack: list[MutableMapping[str, object]] = [schema]
    while stack:
        node = stack.pop()
        properties_raw = node.get("properties")
        properties_map = _ensure_mutable_mapping(properties_raw)
        if properties_map is not None:
            node["properties"] = properties_map
            for prop_name, prop_value in list(properties_map.items()):
                translation = properties_payload.get(prop_name)
                if translation:
                    target_mapping = _ensure_mutable_mapping(prop_value)
                    if target_mapping is None:
                        continue
                    properties_map[prop_name] = target_mapping
                    _apply_translation_recursive(
                        target_mapping, translation, locale, config
                    )
                    stack.append(target_mapping)
                else:
                    target_mapping = _ensure_mutable_mapping(prop_value)
                    if target_mapping is not None:
                        properties_map[prop_name] = target_mapping
                        stack.append(target_mapping)

        items_raw = node.get("items")
        items_map = _ensure_mutable_mapping(items_raw)
        if items_map is not None:
            node["items"] = items_map
            stack.append(items_map)

        additional_raw = node.get("additionalProperties")
        additional_map = _ensure_mutable_mapping(additional_raw)
        if additional_map is not None:
            node["additionalProperties"] = additional_map
            stack.append(additional_map)

        for array_key in ARRAY_TRANSLATABLE_KEYS:
            array_value = node.get(array_key)
            if isinstance(array_value, list):
                new_array: list[object] = []
                changed = False
                for element in array_value:
                    element_map = _ensure_mutable_mapping(element)
                    if element_map is not None:
                        new_array.append(element_map)
                        stack.append(element_map)
                        if element_map is not element:
                            changed = True
                    else:
                        new_array.append(element)
                if changed:
                    node[array_key] = new_array

        definitions_raw = node.get("definitions")
        definitions_map = _ensure_mutable_mapping(definitions_raw)
        if definitions_map is not None:
            node["definitions"] = definitions_map
            for def_name, def_value in list(definitions_map.items()):
                def_map = _ensure_mutable_mapping(def_value)
                if def_map is not None:
                    definitions_map[def_name] = def_map
                    stack.append(def_map)


def _should_strip_key(key: str, strip_keys: Tuple[str, ...]) -> bool:
    for candidate in strip_keys:
        if not candidate:
            continue
        if candidate == key:
            return True
        if candidate.endswith("*") and key.startswith(candidate[:-1]):
            return True
    return False


ALLOWED_DEFINITION_KEYS = {
    "$id",
    "$ref",
    "type",
    "description",
    "allOf",
    "properties",
    "enum",
    "required",
    "dependencies",
    "discriminator",
}


PROPERTY_DESCRIPTION_TEMPLATE = (
    "{name} description is missing in the official specification."
)


def ensure_property_descriptions(schema: MutableMapping[str, object]) -> None:
    stack: list[MutableMapping[str, object]] = [schema]
    while stack:
        node = stack.pop()

        properties = node.get("properties")
        if isinstance(properties, Mapping):
            if not isinstance(properties, MutableMapping):
                mutable_props: MutableMapping[str, object] = dict(properties)
                node["properties"] = mutable_props
                properties = mutable_props
            for prop_name, prop_value in list(properties.items()):
                if not isinstance(prop_value, Mapping):
                    continue
                if not isinstance(prop_value, MutableMapping):
                    mutable_prop: MutableMapping[str, object] = dict(prop_value)
                    properties[prop_name] = mutable_prop
                    prop_map = mutable_prop
                else:
                    prop_map = prop_value

                description = prop_map.get("description")
                if not isinstance(description, str) or not description.strip():
                    prop_map["description"] = PROPERTY_DESCRIPTION_TEMPLATE.format(
                        name=prop_name
                    )
                stack.append(prop_map)

        for key in ("items", "additionalProperties", "allOf", "anyOf", "oneOf"):
            child = node.get(key)
            if isinstance(child, Mapping):
                if not isinstance(child, MutableMapping):
                    mutable_child: MutableMapping[str, object] = dict(child)
                    node[key] = mutable_child
                    stack.append(mutable_child)
                else:
                    stack.append(child)
            elif isinstance(child, list):
                for index, item in enumerate(child):
                    if isinstance(item, Mapping):
                        if not isinstance(item, MutableMapping):
                            mutable_item: MutableMapping[str, object] = dict(item)
                            child[index] = mutable_item
                            stack.append(mutable_item)
                        else:
                            stack.append(item)


def sanitize_for_validation(
    payload: object,
    strip_keys: Tuple[str, ...],
    *,
    context: str = "root",
) -> object:
    if isinstance(payload, dict):
        sanitized: Dict[str, object] = {}
        if context == "definitions" and payload:
            for def_name, def_value in payload.items():
                if not isinstance(def_value, Mapping):
                    continue
                sanitized[def_name] = sanitize_for_validation(
                    def_value, strip_keys, context="definition"
                )
            return sanitized

        for key, value in payload.items():
            if _should_strip_key(key, strip_keys):
                continue

            if context == "definition" and key not in ALLOWED_DEFINITION_KEYS:
                continue

            if key == "definitions" and isinstance(value, Mapping):
                sanitized[key] = sanitize_for_validation(
                    value, strip_keys, context="definitions"
                )
                continue

            sanitized[key] = sanitize_for_validation(value, strip_keys, context="generic")

        if context == "definition":
            if "type" not in sanitized:
                if "enum" in sanitized and "$ref" not in sanitized:
                    sanitized["type"] = "string"
                elif any(
                    key in sanitized
                    for key in ("properties", "allOf", "anyOf", "oneOf", "dependencies", "$ref")
                ):
                    sanitized["type"] = "object"
        return sanitized
    if isinstance(payload, list):
        return [sanitize_for_validation(item, strip_keys, context="generic") for item in payload]
    return payload


def _pick_translation_payload(
    translations: Mapping[str, Mapping[str, object]], schema_name: str
) -> Optional[Mapping[str, object]]:
    for alias in iter_model_aliases(schema_name):
        payload = translations.get(alias)
        if payload:
            return payload
    return None


def apply_locale_translations(
    schema: MutableMapping[str, object],
    schema_name: str,
    locale_translations: Tuple[Tuple[LocaleConfig, Mapping[str, Mapping[str, object]]], ...],
    config: PipelineConfig,
) -> Tuple[str, ...]:
    applied: list[str] = []
    for locale, translations in locale_translations:
        applied_this_locale = False

        global_payload = translations.get(GLOBAL_PROPERTY_TRANSLATION_KEY)
        if global_payload:
            residual, property_payload = _split_property_translations(global_payload)
            if property_payload:
                apply_property_translations_to_schema(
                    schema, property_payload, locale, config
                )
                applied_this_locale = True
            if residual:
                _apply_translation_recursive(schema, residual, locale, config)
                applied_this_locale = True

        translation_payload = _pick_translation_payload(translations, schema_name)
        if translation_payload:
            residual, property_payload = _split_property_translations(translation_payload)
            if property_payload:
                apply_property_translations_to_schema(
                    schema, property_payload, locale, config
                )
                applied_this_locale = True
            if residual:
                _apply_translation_recursive(schema, residual, locale, config)
                applied_this_locale = True

        if applied_this_locale:
            applied.append(locale.output_code)
    return tuple(applied)


def _extract_major_version(version: Optional[str], path: Path) -> Optional[str]:
    if version:
        major = version.split(".")[0].strip()
        if major:
            return major.lower()
    for part in path.parts:
        lower = part.lower()
        if lower.startswith("api-v"):
            return lower.split("-", 1)[-1]
    return None


def analyze_spec_files(
    spec_files: Iterable[Path],
    pattern: re.Pattern[str],
    allowed_major_versions: Tuple[str, ...] = (),
) -> tuple[list[SpecDocument], Counter]:
    documents: list[SpecDocument] = []
    schema_counts: Counter = Counter()

    for spec_file in sorted(spec_files):
        match = pattern.match(spec_file.stem)
        if not match:
            continue

        payload = load_spec_file(spec_file)
        if payload is None:
            continue

        components = payload.get("components") if isinstance(payload, Mapping) else None
        schemas: Mapping[str, object] = {}
        if isinstance(components, Mapping):
            raw_schemas = components.get("schemas") or {}
            if isinstance(raw_schemas, Mapping):
                schemas = raw_schemas

        if not schemas:
            definitions = payload.get("definitions") if isinstance(payload, Mapping) else None
            if isinstance(definitions, Mapping):
                schemas = definitions

        if not isinstance(schemas, Mapping) or not schemas:
            continue

        version = match.group(2) if match.lastindex and match.lastindex >= 2 else None
        major_version = _extract_major_version(version, spec_file)

        if allowed_major_versions and (
            major_version is None or major_version not in allowed_major_versions
        ):
            continue

        document = SpecDocument(
            api_code=match.group(1).upper(),
            version=version,
            major_version=major_version,
            protocol=detect_protocol(spec_file),
            file=spec_file,
            schemas=schemas,
        )

        documents.append(document)

        unique_names = {canonical_model_name(name) for name in schemas.keys()}
        schema_counts.update(unique_names)

    return documents, schema_counts


def infer_domain(
    model_name: str,
    schema_counts: Mapping[str, int],
    domain_mapping: Mapping[str, str],
    api_code: Optional[str],
    config: PipelineConfig,
) -> str:
    if schema_counts.get(model_name, 0) >= config.common_candidate_threshold:
        return "Common"
    if api_code and api_code in domain_mapping:
        return domain_mapping[api_code]
    return config.default_domain


def _extract_schema_ref_name(ref: str) -> Optional[str]:
    if not ref.startswith("#/"):
        return None
    parts = ref.split("/")
    if len(parts) < 3:
        return None
    if parts[1].lower() == "components" and len(parts) >= 4 and parts[2].lower() == "schemas":
        return parts[-1]
    if parts[1].lower() == "definitions":
        return parts[-1]
    return None


def resolve_refs(
    obj: object, current_location: ModelLocation, model_locations: Mapping[str, ModelLocation]
) -> object:
    if isinstance(obj, dict):
        new_obj: Dict[str, object] = {}
        for key, value in obj.items():
            if key == "$ref" and isinstance(value, str):
                ref_name = _extract_schema_ref_name(value)
                if ref_name is None:
                    new_obj[key] = value
                    continue
                target_location = model_locations.get(ref_name)
                if target_location is None:
                    target_location = model_locations.get(canonical_model_name(ref_name))
                if target_location is None:
                    target_location = current_location
                prefix = _relative_ref_prefix(current_location, target_location)
                new_obj[key] = f"{prefix}{ref_name}.schema.json#{ref_name}"
            else:
                new_obj[key] = resolve_refs(value, current_location, model_locations)
        return new_obj
    if isinstance(obj, list):
        return [resolve_refs(item, current_location, model_locations) for item in obj]
    return obj


def normalize_schema_structure(schema: MutableMapping[str, object]) -> None:
    stack: list[MutableMapping[str, object]] = [schema]
    while stack:
        node = stack.pop()

        discriminator = node.get("discriminator")
        if isinstance(discriminator, Mapping):
            property_name = discriminator.get("propertyName")
            if isinstance(property_name, str) and property_name.strip():
                node["discriminator"] = property_name.strip()
            else:
                node.pop("discriminator", None)
        elif discriminator is None:
            node.pop("discriminator", None)

        if "nullable" in node:
            node.pop("nullable", None)

        if "type" not in node:
            if any(key in node for key in ("properties", "allOf", "anyOf", "oneOf", "required", "dependencies")):
                node["type"] = "object"

        for key, value in list(node.items()):
            if isinstance(value, Mapping):
                if not isinstance(value, MutableMapping):
                    mutable_value: MutableMapping[str, object] = dict(value)
                    node[key] = mutable_value
                    stack.append(mutable_value)
                else:
                    stack.append(value)
            elif isinstance(value, list):
                new_list: list[object] = []
                replaced = False
                for item in value:
                    if isinstance(item, Mapping):
                        if not isinstance(item, MutableMapping):
                            mutable_item: MutableMapping[str, object] = dict(item)
                            new_list.append(mutable_item)
                            stack.append(mutable_item)
                            replaced = True
                        else:
                            new_list.append(item)
                            stack.append(item)
                    else:
                        new_list.append(item)
                if replaced:
                    node[key] = new_list


def build_schema_document(
    model_name: str,
    location: ModelLocation,
    schema: Mapping[str, object],
    config: PipelineConfig,
    source: SpecDocument,
    usage_count: int,
    repo_root: Path,
    locales: Optional[Iterable[str]] = None,
) -> dict:
    try:
        relative_path = source.file.resolve().relative_to(repo_root)
    except ValueError:
        relative_path = source.file.resolve()

    metadata: Dict[str, object] = {
        "domain": location.domain,
        "usage": {"occurrences": usage_count},
        "source": {
            "apiCode": source.api_code,
            "version": source.version,
            "protocol": source.protocol,
            "filename": source.file.name,
            "path": str(relative_path),
        },
    }

    container_info: Dict[str, object] = {
        "path": location.path.as_posix(),
        "domain": location.domain,
    }
    if location.api_code:
        container_info["apiCode"] = location.api_code
        if location.api_name:
            container_info["apiName"] = location.api_name
    if location.extra_segments:
        container_info["subfolders"] = list(location.extra_segments)
    metadata["container"] = container_info

    if locales:
        unique_locales = sorted({code for code in locales if code})
        if unique_locales:
            metadata["i18n"] = {
                "locales": unique_locales,
            }
            if config.i18n_default_locale:
                metadata["i18n"]["defaultLocale"] = config.i18n_default_locale

    return {
        "$schema": config.schema_draft,
        "$id": f"{model_name}.schema.json",
        "title": model_name,
        "x-metadata": metadata,
        "definitions": {
            model_name: {
                "$id": f"#{model_name}",
                **schema,
            }
        },
    }


def write_schema_file(
    output_root: Path, location: ModelLocation, model_name: str, document: Mapping[str, object]
) -> Path:
    target_dir = location.filesystem_dir(output_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{model_name}.schema.json"
    with target_path.open("w", encoding="utf-8") as fh:
        json.dump(document, fh, ensure_ascii=False, indent=2)
    return target_path


def run_pipeline(config: PipelineConfig, clean: bool = False) -> None:
    domain_mapping = load_domain_mapping(config.domain_mapping_file)
    api_name_mapping = load_api_name_mapping(config.api_name_mapping_file)
    schema_taxonomy = load_schema_taxonomy(config.schema_taxonomy_file)

    spec_sources = list(collect_spec_sources([config.tmf_official, config.external]))
    documents, schema_counts = analyze_spec_files(
        spec_sources,
        config.filename_pattern,
        config.allowed_major_versions,
    )
    locale_translations = load_all_translations(config.i18n_locales)

    if clean and config.output_root.exists():
        shutil.rmtree(config.output_root)
    if clean and config.organized_root.exists():
        shutil.rmtree(config.organized_root)
    if clean and config.validation_root and config.validation_root.exists():
        shutil.rmtree(config.validation_root)

    repo_root = Path.cwd()
    written_models: Dict[str, SpecDocument] = {}
    location_cache: Dict[str, ModelLocation] = {}
    model_location_map: Dict[str, ModelLocation] = {}
    organized_records: list[OrganizedSchemaRecord] = []

    for canonical_name, placement in schema_taxonomy.canonical_items():
        location = build_model_location(
            placement.domain or config.default_domain,
            placement.api_code,
            api_name_mapping,
            placement.extra_segments,
            config.default_domain,
        )
        location_cache[canonical_name] = location
        for alias in iter_model_aliases(canonical_name):
            model_location_map.setdefault(alias, location)

    def document_sort_key(doc: SpecDocument) -> tuple[int, str, str, str]:
        priority = PROTOCOL_PRIORITY.get(doc.protocol, 9)
        version = doc.version or ""
        return (priority, doc.api_code, version, doc.file.name)

    for document in sorted(documents, key=document_sort_key):
        for schema_name, schema_def in document.schemas.items():
            canonical_name = canonical_model_name(schema_name)
            previous = written_models.get(schema_name)
            if previous is not None:
                current_priority = PROTOCOL_PRIORITY.get(document.protocol, 9)
                previous_priority = PROTOCOL_PRIORITY.get(previous.protocol, 9)
                if current_priority > previous_priority:
                    continue

            location = determine_model_location(
                schema_name,
                canonical_name,
                document,
                schema_taxonomy,
                domain_mapping,
                schema_counts,
                config,
                api_name_mapping,
                location_cache,
            )

            for alias in iter_model_aliases(schema_name):
                model_location_map[alias] = location

            resolved = resolve_refs(deepcopy(schema_def), location, model_location_map)
            prepared_schema = resolved
            applied_locales: Tuple[str, ...] = ()

            mutable_schema = _ensure_mutable_mapping(resolved)
            if mutable_schema is not None:
                applied_locales = apply_locale_translations(
                    mutable_schema,
                    schema_name,
                    locale_translations,
                    config,
                )
                normalize_schema_structure(mutable_schema)
                ensure_property_descriptions(mutable_schema)
                prepared_schema = mutable_schema

            document_payload = build_schema_document(
                schema_name,
                location,
                prepared_schema,
                config,
                document,
                schema_counts.get(canonical_name, 0),
                repo_root,
                applied_locales,
            )
            output_path = write_schema_file(config.output_root, location, schema_name, document_payload)
            if config.validation_root:
                sanitized_payload = sanitize_for_validation(
                    document_payload, config.validation_strip_keys
                )
                write_schema_file(
                    config.validation_root, location, schema_name, sanitized_payload
                )
            organized_records.append(
                OrganizedSchemaRecord(
                    model_name=schema_name,
                    source_path=output_path,
                    container=location.path,
                    domain=location.domain,
                    api_code=location.api_code or document.api_code,
                    api_name=location.api_name,
                    version=document.version,
                    major_version=document.major_version,
                    protocol=document.protocol,
                )
            )
            written_models[schema_name] = document

    if organized_records:
        organize_schema_outputs(config, organized_records)

    print(f"✅ 已输出 {len(written_models)} 个模型定义到 {config.output_root}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="官方 YAML -> 企业 schema 的构建流水线")
    parser.add_argument(
        "--config",
        default="pipeline.config.yaml",
        type=Path,
        help="配置文件路径，默认使用仓库根目录的 pipeline.config.yaml",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="执行前清空输出目录",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = PipelineConfig.load(args.config)
    run_pipeline(config, clean=args.clean)


if __name__ == "__main__":
    main()
