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
import json
import re
import shutil
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, MutableMapping, Optional, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover - 仅在缺失依赖时执行
    raise SystemExit(
        "未安装 PyYAML，请先执行 `pip install pyyaml` 或在虚拟环境中添加依赖。"
    ) from exc


@dataclass
class SpecDocument:
    """封装单个规范文件的解析结果。"""

    api_code: str
    version: Optional[str]
    protocol: str
    file: Path
    schemas: Mapping[str, object]


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
class PipelineConfig:
    """结构化后的配置对象。"""

    tmf_official: Path
    external: Path
    name_mapping: Path
    ground_truth: Path
    common_candidate_threshold: int
    filename_pattern: re.Pattern[str]
    prefer_existing_domains: bool
    default_domain: str
    output_root: Path
    output_docs: Path
    output_yaml: Path
    domain_mapping_file: Path
    schema_draft: str
    i18n_locales: Tuple[LocaleConfig, ...] = ()
    i18n_default_locale: str = "en"
    i18n_output_key: str = "x-i18n"

    @classmethod
    def load(cls, config_path: Path) -> "PipelineConfig":
        if not config_path.exists():
            raise FileNotFoundError(f"未找到配置文件: {config_path}")
        with config_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        sources = raw.get("sources", {})
        processing = raw.get("processing", {})
        output = raw.get("output", {})
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

        return cls(
            tmf_official=Path(sources.get("tmf_official", "sources/tmf-official")),
            external=Path(sources.get("external", "sources/external")),
            name_mapping=Path(sources.get("name_mapping", "config/name_mapping.json")),
            ground_truth=Path(sources.get("ground_truth", ".")),
            common_candidate_threshold=int(processing.get("common_candidate_threshold", 12)),
            filename_pattern=re.compile(processing.get("filename_regex", r"^(TMF\\d+)")),
            prefer_existing_domains=bool(processing.get("prefer_existing_domains", True)),
            default_domain=str(processing.get("default_domain", "Unclassified")),
            output_root=Path(output.get("root", "dist/json")),
            output_docs=Path(output.get("docs", "dist/docs")),
            output_yaml=Path(output.get("yaml", "dist/yaml")),
            domain_mapping_file=Path(metadata.get("domain_mapping_file", "config/domain_mapping.yaml")),
            schema_draft=str(metadata.get("schema_draft", "http://json-schema.org/draft-07/schema#")),
            i18n_locales=tuple(locales),
            i18n_default_locale=i18n_default_locale,
            i18n_output_key=output_key,
        )


def load_json(path: Path) -> MutableMapping[str, str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh) or {}


def load_domain_mapping(path: Path) -> Mapping[str, str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return {str(k): str(v) for k, v in data.items()}


def scan_ground_truth(directory: Path, name_mapping: Mapping[str, str]) -> Mapping[str, str]:
    """扫描现有 JSON schema，构建模型 -> 域 的映射基线。"""
    reverse_name_map = {v: k for k, v in name_mapping.items()}
    ground_truth_map: Dict[str, str] = {}

    if not directory.exists():
        return ground_truth_map

    for domain_path in directory.iterdir():
        if not domain_path.is_dir():
            continue
        for schema_file in domain_path.glob("*.schema.json"):
            schema_name = schema_file.stem
            normalized = reverse_name_map.get(schema_name, schema_name)
            ground_truth_map[normalized] = domain_path.name
    return ground_truth_map


def collect_spec_sources(paths: Iterable[Path]) -> Iterable[Path]:
    suffixes = {".yaml", ".yml", ".json"}
    for base in paths:
        if not base.exists():
            continue
        for file in base.rglob("*"):
            if file.is_file() and file.suffix.lower() in suffixes:
                yield file


def load_spec_file(path: Path) -> Optional[Mapping[str, object]]:
    loader = json.load if path.suffix.lower() == ".json" else yaml.safe_load
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = loader(fh) or {}
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        print(f"⚠️  解析失败 {path}: {exc}")
        return None
    return data if isinstance(data, Mapping) else None


def load_translation_file(path: Path) -> Optional[Mapping[str, object]]:
    loader = json.load if path.suffix.lower() == ".json" else yaml.safe_load
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = loader(fh) or {}
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
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


TRANSLATABLE_FIELDS = {"title", "description", "summary"}
ARRAY_TRANSLATABLE_KEYS = {"allOf", "anyOf", "oneOf"}


def load_locale_translations(locale: LocaleConfig) -> Mapping[str, Mapping[str, object]]:
    if not locale.directory.exists():
        return {}

    translations: Dict[str, Mapping[str, object]] = {}
    for file in locale.directory.rglob("*"):
        if not file.is_file() or file.suffix.lower() not in {".yaml", ".yml", ".json"}:
            continue
        payload = load_translation_file(file)
        if not payload:
            continue

        schema_name = payload.get("schema") or payload.get("model") or payload.get("name")
        if isinstance(schema_name, str) and schema_name.strip():
            key = schema_name.strip()
        else:
            key = file.stem

        if "translations" in payload and isinstance(payload["translations"], Mapping):
            translations[key] = payload["translations"]  # type: ignore[assignment]
        else:
            filtered = {
                k: v
                for k, v in payload.items()
                if k not in {"schema", "model", "name"}
            }
            if filtered:
                translations[key] = filtered
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


def apply_locale_translations(
    schema: MutableMapping[str, object],
    model_name: str,
    locale_translations: Tuple[Tuple[LocaleConfig, Mapping[str, Mapping[str, object]]], ...],
    config: PipelineConfig,
) -> Tuple[str, ...]:
    applied: list[str] = []
    for locale, translations in locale_translations:
        translation_payload = translations.get(model_name)
        if not translation_payload:
            continue
        _apply_translation_recursive(schema, translation_payload, locale, config)
        applied.append(locale.output_code)
    return tuple(applied)


def analyze_spec_files(spec_files: Iterable[Path], pattern: re.Pattern[str]) -> tuple[list[SpecDocument], Counter]:
    documents: list[SpecDocument] = []
    schema_counts: Counter = Counter()

    for spec_file in sorted(spec_files):
        match = pattern.match(spec_file.stem)
        if not match:
            continue

        payload = load_spec_file(spec_file)
        if payload is None:
            continue

        components = (payload.get("components") if isinstance(payload, Mapping) else None) or {}
        schemas = components.get("schemas") or {}
        if not isinstance(schemas, Mapping) or not schemas:
            continue

        version = match.group(2) if match.lastindex and match.lastindex >= 2 else None
        document = SpecDocument(
            api_code=match.group(1),
            version=version,
            protocol=detect_protocol(spec_file),
            file=spec_file,
            schemas=schemas,
        )

        documents.append(document)

        unique_names = {name.split("_")[0] for name in schemas.keys()}
        schema_counts.update(unique_names)

    return documents, schema_counts


def infer_domain(
    model_name: str,
    ground_truth_map: Mapping[str, str],
    schema_counts: Mapping[str, int],
    domain_mapping: Mapping[str, str],
    api_code: Optional[str],
    config: PipelineConfig,
) -> str:
    if config.prefer_existing_domains and model_name in ground_truth_map:
        return ground_truth_map[model_name]
    if schema_counts.get(model_name, 0) >= config.common_candidate_threshold:
        return "Common"
    if api_code and api_code in domain_mapping:
        return domain_mapping[api_code]
    return config.default_domain


def resolve_refs(obj: object, current_domain: str, model_domain_map: Mapping[str, str]) -> object:
    if isinstance(obj, dict):
        new_obj = {}
        for key, value in obj.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/components/schemas/"):
                ref_name = value.split("/")[-1].split("_")[0]
                ref_domain = model_domain_map.get(ref_name, current_domain)
                prefix = "" if ref_domain == current_domain else f"../{ref_domain}/"
                new_obj[key] = f"{prefix}{ref_name}.schema.json#{ref_name}"
            else:
                new_obj[key] = resolve_refs(value, current_domain, model_domain_map)
        return new_obj
    if isinstance(obj, list):
        return [resolve_refs(item, current_domain, model_domain_map) for item in obj]
    return obj


def build_schema_document(
    model_name: str,
    domain: str,
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
        "domain": domain,
        "usage": {"occurrences": usage_count},
        "source": {
            "apiCode": source.api_code,
            "version": source.version,
            "protocol": source.protocol,
            "filename": source.file.name,
            "path": str(relative_path),
        },
    }

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


def write_schema_file(output_root: Path, domain: str, model_name: str, document: Mapping[str, object]) -> None:
    target_dir = output_root / domain
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{model_name}.schema.json"
    with target_path.open("w", encoding="utf-8") as fh:
        json.dump(document, fh, ensure_ascii=False, indent=2)


def run_pipeline(config: PipelineConfig, clean: bool = False) -> None:
    name_mapping = load_json(config.name_mapping)
    ground_truth_map = scan_ground_truth(config.ground_truth, name_mapping)
    domain_mapping = load_domain_mapping(config.domain_mapping_file)

    spec_sources = list(collect_spec_sources([config.tmf_official, config.external]))
    documents, schema_counts = analyze_spec_files(spec_sources, config.filename_pattern)
    locale_translations = load_all_translations(config.i18n_locales)

    if clean and config.output_root.exists():
        shutil.rmtree(config.output_root)

    model_domain_map: Dict[str, str] = dict(ground_truth_map)
    repo_root = Path.cwd()
    written_models: Dict[str, SpecDocument] = {}

    def document_sort_key(doc: SpecDocument) -> tuple[int, str, str, str]:
        priority = PROTOCOL_PRIORITY.get(doc.protocol, 9)
        version = doc.version or ""
        return (priority, doc.api_code, version, doc.file.name)

    for document in sorted(documents, key=document_sort_key):
        for schema_name, schema_def in document.schemas.items():
            model_name = schema_name.split("_")[0]
            domain = infer_domain(
                model_name,
                ground_truth_map,
                schema_counts,
                domain_mapping,
                document.api_code,
                config,
            )
            previous = written_models.get(model_name)
            if previous is not None:
                current_priority = PROTOCOL_PRIORITY.get(document.protocol, 9)
                previous_priority = PROTOCOL_PRIORITY.get(previous.protocol, 9)
                if current_priority > previous_priority:
                    continue

            model_domain_map[model_name] = domain
            resolved = resolve_refs(deepcopy(schema_def), domain, model_domain_map)
            applied_locales: Tuple[str, ...] = ()
            if isinstance(resolved, MutableMapping):
                applied_locales = apply_locale_translations(
                    resolved,
                    model_name,
                    locale_translations,
                    config,
                )
            document_payload = build_schema_document(
                model_name,
                domain,
                resolved,
                config,
                document,
                schema_counts.get(model_name, 0),
                repo_root,
                applied_locales,
            )
            write_schema_file(config.output_root, domain, model_name, document_payload)
            written_models[model_name] = document

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
