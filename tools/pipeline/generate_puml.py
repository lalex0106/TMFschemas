#!/usr/bin/env python3
"""从企业 Schema 仓库生成 PlantUML ER 图的辅助脚本。

该脚本主要面向已经通过流水线生成的 `dist/json` 目录，可自动解析
`*.schema.json` 文件中定义的属性、引用关系，并输出结构化的 PlantUML
文本，便于架构师与业务分析人员快速浏览模型之间的关联。脚本提供
以下特性：

* **动态关系发现**：遍历属性中包含的 `$ref`，自动识别实体间关联；
* **递归探索**：以广度优先方式在限定深度内展开关联实体，避免图谱
  过度膨胀；
* **基数推断**：根据属性是否为数组判断一对一或一对多，并输出合适
  的连线样式；
* **命令行参数**：可自定义起始实体、探索深度、输出文件与 Schema 仓
  库路径，支持生成局部或全局的视图。

示例：

```bash
# 生成围绕 Product / Service / Customer 的概览图
python tools/pipeline/generate_puml.py -o overview.puml

# 指定实体并限制探索深度
python tools/pipeline/generate_puml.py -r Product Catalog --depth 1 -o product_view.puml
```
"""

from __future__ import annotations

import argparse
import json
import re
from collections import deque
import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Set, Tuple

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - 按需提示
    yaml = None  # type: ignore[assignment]


DEFAULT_REPO_CANDIDATES = (
    Path("dist/json"),
    Path("dist/validation"),
)
DEFAULT_SOURCE_ROOT = Path("sources/tmf-official")
DEFAULT_VERSIONS = ("API-v1", "API-v2", "API-v3", "API-v4", "API-v5")
DEFAULT_CORE_ENTITIES = ("Product", "Service", "Customer")
DEFAULT_I18N_ROOT = Path("overrides/i18n")
DEFAULT_MAX_DEPTH = 2
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def _normalize_name(value: str) -> str:
    """将名称统一为大小写不敏感且忽略分隔符的索引键。"""

    compact = re.sub(r"[\s_\-]+", "", value)
    return compact.lower()


def _select_locale_key(language: Optional[str], available: Iterable[str]) -> Optional[str]:
    """在现有 locale key 中查找与请求语言最匹配的键。"""

    if not language:
        return None

    normalized = language.lower()
    candidates = [normalized]
    compact = normalized.replace("-", "").replace("_", "")
    if compact not in candidates:
        candidates.append(compact)
    alt_dash = normalized.replace("_", "-")
    if alt_dash not in candidates:
        candidates.append(alt_dash)
    alt_underscore = normalized.replace("-", "_")
    if alt_underscore not in candidates:
        candidates.append(alt_underscore)
    if "-" in normalized:
        prefix = normalized.split("-", 1)[0]
        if prefix not in candidates:
            candidates.append(prefix)
    if "_" in normalized:
        prefix = normalized.split("_", 1)[0]
        if prefix not in candidates:
            candidates.append(prefix)

    lowered_map = {key.lower(): key for key in available}
    for candidate in candidates:
        if candidate in lowered_map:
            return lowered_map[candidate]

    for candidate in candidates:
        for key in lowered_map.keys():
            if key.startswith(candidate):
                return lowered_map[key]

    return None


def _format_localized_label(
    base: str,
    primary_label: Optional[str],
    secondary_label: Optional[str],
    bilingual: bool,
    primary_language: Optional[str],
) -> str:
    """根据主语言/回退语言组合出最终展示文本。"""

    if bilingual:
        main = primary_label or base
        fallback = secondary_label or (base if main != base else None)
        if fallback and fallback != main:
            return f"{main} ({fallback})"
        return main

    if primary_label:
        if (
            primary_language
            and not primary_language.lower().startswith("en")
            and primary_label != base
        ):
            return f"{primary_label} ({base})"
        return primary_label

    if secondary_label:
        if secondary_label != base:
            return f"{secondary_label} ({base})"
        return secondary_label

    return base


@dataclass(frozen=True)
class SchemaRecord:
    """存储单个 Schema 的元信息与内容。"""

    name: str
    domain: str
    path: Path
    payload: Mapping[str, object]
    title: Optional[str] = None
    translations: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    def definition(self) -> Optional[Mapping[str, object]]:
        """获取与 Schema 同名的 definition 内容。"""

        definitions = self.payload.get("definitions")
        if not isinstance(definitions, Mapping):
            return None

        candidate = definitions.get(self.name)
        if isinstance(candidate, Mapping):
            return candidate

        # 兜底：取 definitions 中的第一个实体。
        for value in definitions.values():
            if isinstance(value, Mapping):
                return value
        return None
    def aliases(self) -> Set[str]:
        """返回可供匹配的名称别名集合。"""

        names: Set[str] = {self.name}

        if isinstance(self.title, str) and self.title:
            names.add(self.title)

        for locale in self.translations.values():
            title = locale.get("title") if isinstance(locale, Mapping) else None
            if isinstance(title, str) and title:
                names.add(title)

        return {alias for alias in names if alias}

    def _locale_payload(self, language: Optional[str]) -> Optional[Mapping[str, object]]:
        """根据语言代码匹配本地化信息。"""

        key = _select_locale_key(language, self.translations.keys())
        if not key:
            return None
        payload = self.translations.get(key)
        return payload if isinstance(payload, Mapping) else None

    def localized_title(
        self,
        primary_language: Optional[str],
        fallback_language: Optional[str],
        bilingual: bool,
    ) -> str:
        """根据语言偏好生成展示名称。"""

        base_name = self.title or self.name

        def _pick(language: Optional[str]) -> Optional[str]:
            payload = self._locale_payload(language)
            if payload:
                candidate = payload.get("title")
                if isinstance(candidate, str) and candidate:
                    return candidate
            if language and language.lower().startswith("en"):
                return base_name
            return None

        primary = _pick(primary_language)
        secondary = _pick(fallback_language)

        return _format_localized_label(
            base_name,
            primary,
            secondary,
            bilingual,
            primary_language,
        )

    def _property_label_for_language(
        self,
        repository: "SchemaRepository",
        prop_name: str,
        language: Optional[str],
    ) -> Optional[str]:
        if not language:
            return None

        payload = self._locale_payload(language)
        if payload:
            props = payload.get("properties")
            if isinstance(props, Mapping):
                entry = props.get(prop_name)
                if isinstance(entry, Mapping):
                    for key in ("title", "name"):
                        value = entry.get(key)
                        if isinstance(value, str) and value:
                            return value
                elif isinstance(entry, str) and entry:
                    return entry

        return repository.lookup_global_property(language, prop_name)

    def property_display_name(
        self,
        repository: "SchemaRepository",
        prop_name: str,
        primary_language: Optional[str],
        fallback_language: Optional[str],
        bilingual: bool,
    ) -> str:
        base_name = prop_name
        primary = self._property_label_for_language(
            repository, prop_name, primary_language
        )
        secondary = self._property_label_for_language(
            repository, prop_name, fallback_language
        )
        return _format_localized_label(
            base_name,
            primary,
            secondary,
            bilingual,
            primary_language,
        )


class SchemaRepository:
    """帮助在内存中索引全部 Schema。"""

    def __init__(
        self,
        global_property_translations: Optional[
            Mapping[str, Mapping[str, object]]
        ] = None,
    ) -> None:
        self._records: Dict[Tuple[str, str], SchemaRecord] = {}
        self._aliases: Dict[str, Set[Tuple[str, str]]] = {}
        self._global_properties: Dict[str, Dict[str, Mapping[str, object]]] = {}

        if global_property_translations:
            for locale, mapping in global_property_translations.items():
                if not isinstance(mapping, Mapping):
                    continue
                entries: Dict[str, Mapping[str, object]] = {}
                for prop_name, payload in mapping.items():
                    if isinstance(payload, Mapping):
                        entries[prop_name] = {
                            key: value
                            for key, value in payload.items()
                            if isinstance(key, str)
                        }
                    elif isinstance(payload, str) and payload:
                        entries[prop_name] = {"title": payload}
                if entries:
                    self._global_properties[locale] = entries

    def add(self, record: SchemaRecord) -> None:
        key = (record.domain, record.name)
        self._records[key] = record
        for alias in record.aliases():
            norm = _normalize_name(alias)
            self._aliases.setdefault(norm, set()).add(key)

    def get(self, name: str, domain: Optional[str] = None) -> Optional[SchemaRecord]:
        if domain:
            record = self._records.get((domain, name))
            if record:
                return record

        norm = _normalize_name(name)
        candidates = self._aliases.get(norm)
        if not candidates:
            return None

        if domain:
            for candidate in candidates:
                if candidate[0] == domain:
                    return self._records.get(candidate)

        if len(candidates) == 1:
            return self._records[next(iter(candidates))]

        # 若存在多个候选，优先选择名称完全一致的记录。
        for candidate in sorted(candidates):
            record = self._records[candidate]
            if _normalize_name(record.name) == norm:
                return record
        return self._records[sorted(candidates)[0]]

    def contains(self, name: str) -> bool:
        norm = _normalize_name(name)
        return norm in self._aliases

    def list_aliases(self) -> Dict[str, Set[str]]:
        result: Dict[str, Set[str]] = {}
        for (domain, name), record in self._records.items():
            result.setdefault(domain, set()).add(name)
            if isinstance(record.title, str) and record.title:
                result[domain].add(record.title)
            for locale in record.translations.values():
                if isinstance(locale, Mapping):
                    alias = locale.get("title")
                    if isinstance(alias, str) and alias:
                        result[domain].add(alias)
        return result

    def __len__(self) -> int:  # pragma: no cover - 仅用于提示信息
        return len(self._records)

    def lookup_global_property(
        self, language: Optional[str], prop_name: str
    ) -> Optional[str]:
        if not language or not self._global_properties:
            return None

        key = _select_locale_key(language, self._global_properties.keys())
        if not key:
            return None

        locale_mapping = self._global_properties.get(key)
        if not isinstance(locale_mapping, Mapping):
            return None

        entry = locale_mapping.get(prop_name)
        if isinstance(entry, Mapping):
            for field in ("title", "name"):
                value = entry.get(field)
                if isinstance(value, str) and value:
                    return value
        elif isinstance(entry, str) and entry:
            return entry

        return None


def _discover_schema_name(payload: Mapping[str, object], file_path: Path) -> Optional[str]:
    definitions = payload.get("definitions")
    if isinstance(definitions, Mapping):
        for key in definitions.keys():
            if isinstance(key, str) and key:
                return key

    stem = file_path.stem.replace(".schema", "")
    return stem if stem else None


def _extract_translations(payload: Mapping[str, object]) -> Mapping[str, Mapping[str, object]]:
    locales_container = payload.get("x-i18n")
    if not isinstance(locales_container, Mapping):
        return {}

    locales = locales_container.get("locales")
    if isinstance(locales, Mapping):
        return {k: v for k, v in locales.items() if isinstance(v, Mapping)}
    return {}


def _read_csv_rows(path: Path) -> Iterable[Mapping[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                cleaned = {
                    (key.strip() if isinstance(key, str) else key): (
                        value.strip() if isinstance(value, str) else value
                    )
                    for key, value in row.items()
                    if key is not None
                }
                if any(value for value in cleaned.values() if isinstance(value, str)):
                    yield cleaned  # type: ignore[misc]
    except OSError as exc:  # pragma: no cover - 读取异常提示
        print(f"⚠️  无法读取 {path}: {exc}")


def load_translation_overlays(
    i18n_root: Optional[Path],
) -> Tuple[
    Dict[str, Dict[str, Mapping[str, object]]],
    Dict[str, Dict[str, Mapping[str, object]]],
]:
    if not i18n_root or not i18n_root.exists():
        return {}, {}

    schema_translations: Dict[str, Dict[str, Mapping[str, object]]] = {}
    global_properties: Dict[str, Dict[str, Mapping[str, object]]] = {}

    for locale_dir in sorted(i18n_root.iterdir()):
        if not locale_dir.is_dir():
            continue

        locale_code = locale_dir.name

        for csv_path in sorted(locale_dir.glob("Schemas*.csv")):
            for row in _read_csv_rows(csv_path):
                schema_name = row.get("Schema") or row.get("schema")
                if not isinstance(schema_name, str) or not schema_name.strip():
                    continue
                schema_key = schema_name.strip()
                title = row.get("中文名称") or row.get("Chinese Name")
                description = row.get("新描述") or row.get("描述")
                if not (title or description):
                    continue
                schema_entry = schema_translations.setdefault(schema_key, {})
                locale_entry = schema_entry.setdefault(locale_code, {})
                if title:
                    locale_entry["title"] = title
                if description:
                    locale_entry["description"] = description

        for csv_path in sorted(locale_dir.glob("Properties*.csv")):
            for row in _read_csv_rows(csv_path):
                prop_key = row.get("Property") or row.get("property")
                if not isinstance(prop_key, str) or not prop_key.strip():
                    continue
                display_name = row.get("中文名称") or row.get("Chinese Name")
                description = row.get("新描述") or row.get("描述")
                if not (display_name or description):
                    continue

                if "." in prop_key:
                    schema_name, prop_name = prop_key.split(".", 1)
                    schema_name = schema_name.strip()
                    prop_name = prop_name.strip()
                    if not schema_name or not prop_name:
                        continue
                    schema_entry = schema_translations.setdefault(schema_name, {})
                    locale_entry = schema_entry.setdefault(locale_code, {})
                    props_entry = locale_entry.setdefault("properties", {})
                    prop_entry = props_entry.setdefault(prop_name, {})
                    if display_name:
                        prop_entry["title"] = display_name
                    if description:
                        prop_entry["description"] = description
                else:
                    locale_props = global_properties.setdefault(locale_code, {})
                    prop_entry = locale_props.setdefault(prop_key.strip(), {})
                    if display_name:
                        prop_entry["title"] = display_name
                    if description:
                        prop_entry["description"] = description

    return schema_translations, global_properties


def _merge_translations(
    base: Mapping[str, Mapping[str, object]],
    overlay: Mapping[str, Mapping[str, object]],
) -> Dict[str, Mapping[str, object]]:
    result: Dict[str, Mapping[str, object]] = {
        key: dict(value) for key, value in base.items() if isinstance(value, Mapping)
    }

    for locale, payload in overlay.items():
        if not isinstance(payload, Mapping):
            continue
        merged: Dict[str, object] = dict(result.get(locale, {}))
        for key, value in payload.items():
            if key == "properties" and isinstance(value, Mapping):
                existing_props = merged.get("properties")
                props: Dict[str, object] = (
                    dict(existing_props) if isinstance(existing_props, Mapping) else {}
                )
                for prop_name, prop_payload in value.items():
                    if not isinstance(prop_payload, Mapping):
                        continue
                    current = props.get(prop_name)
                    base_mapping = dict(current) if isinstance(current, Mapping) else {}
                    for prop_key, prop_value in prop_payload.items():
                        if isinstance(prop_key, str) and isinstance(prop_value, str) and prop_value:
                            base_mapping[prop_key] = prop_value
                    if base_mapping:
                        props[prop_name] = base_mapping
                if props:
                    merged["properties"] = props
            elif isinstance(value, Mapping):
                merged[key] = dict(value)
            elif isinstance(value, str) and value:
                merged[key] = value
        result[locale] = merged

    return result


def load_repository(repo_root: Path, i18n_root: Optional[Path]) -> SchemaRepository:
    if not repo_root.exists():
        raise SystemExit(f"❌ 未找到 Schema 仓库目录：{repo_root}")

    overlay_schemas, global_properties = load_translation_overlays(i18n_root)
    repository = SchemaRepository(global_properties)
    schema_files = sorted(repo_root.rglob("*.schema.json"))
    if not schema_files:
        raise SystemExit("❌ 指定目录下未发现任何 .schema.json 文件，请确认已执行构建流水线。")

    for file_path in schema_files:
        try:
            with file_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - I/O 异常提示
            print(f"⚠️  无法解析 {file_path}: {exc}")
            continue

        if not isinstance(payload, Mapping):
            continue

        domain = file_path.parent.name
        schema_name = _discover_schema_name(payload, file_path)
        if not schema_name:
            print(f"⚠️  {file_path} 未能识别到 schema 名称，已跳过。")
            continue

        translations = _extract_translations(payload)
        if schema_name in overlay_schemas:
            translations = _merge_translations(translations, overlay_schemas[schema_name])
        record = SchemaRecord(
            schema_name,
            domain,
            file_path,
            payload,
            title=payload.get("title") if isinstance(payload.get("title"), str) else None,
            translations=translations,
        )
        repository.add(record)

    if not len(repository):  # pragma: no cover - 空仓库提示
        raise SystemExit("❌ 未成功加载任何 Schema，目录可能为空或结构异常。")

    print(f"✅ 已加载 {len(repository)} 个 Schema 定义。")
    return repository


@dataclass
class APIDocument:
    """描述单份 API 文档及其资源入口信息。"""

    path: Path
    version: str
    protocol: str
    name: str
    resources: Sequence[str] = field(default_factory=list)
    title: Optional[str] = None

    def resolved_resources(self, repository: SchemaRepository) -> Sequence[str]:
        """将文档中的资源名称映射到仓库内可识别的实体名称。"""

        resolved: list[str] = []
        for candidate in self.resources:
            match = _resolve_repository_name(candidate, repository)
            if match and match not in resolved:
                resolved.append(match)
        return resolved


def _candidate_variants(name: str) -> Iterable[str]:
    trimmed = name.strip()
    if not trimmed:
        return []

    yield trimmed

    no_space = re.sub(r"\s+", "", trimmed)
    if no_space and no_space != trimmed:
        yield no_space

    alnum = re.sub(r"[^0-9A-Za-z]", "", trimmed)
    if alnum and alnum not in {trimmed, no_space}:
        yield alnum

    words = re.split(r"[^0-9A-Za-z]+", trimmed)
    pascal = "".join(part.capitalize() for part in words if part)
    if pascal and pascal not in {trimmed, no_space, alnum}:
        yield pascal

    lower = trimmed.lower()
    if lower.endswith("ies"):
        yield trimmed[:-3] + "y"
    elif lower.endswith("ses"):
        yield trimmed[:-2]
    elif lower.endswith("s"):
        yield trimmed[:-1]


def _resolve_repository_name(name: str, repository: SchemaRepository) -> Optional[str]:
    for variant in _candidate_variants(name):
        record = repository.get(variant)
        if record:
            return record.name
    return None


def _require_yaml() -> None:
    if yaml is None:
        raise SystemExit(
            "❌ 需要 PyYAML 才能解析官方 API 文档，请先执行 `pip install pyyaml` 再重试。"
        )


def _load_api_payload(path: Path) -> Optional[Mapping[str, object]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - I/O 提示
        print(f"⚠️  无法读取 {path}: {exc}")
        return None

    if path.suffix.lower() in {".json"}:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            print(f"⚠️  解析 JSON 失败 {path}: {exc}")
            return None
    else:
        if yaml is None:
            print(
                f"⚠️  跳过 {path}，因为当前环境未安装 PyYAML，无法解析 YAML。"
            )
            return None
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:  # type: ignore[attr-defined]
            print(f"⚠️  解析 YAML 失败 {path}: {exc}")
            return None

    return data if isinstance(data, Mapping) else None


def _extract_resources_from_openapi(payload: Mapping[str, object]) -> Set[str]:
    resources: Set[str] = set()

    tags = payload.get("tags")
    if isinstance(tags, Sequence):
        for item in tags:
            if isinstance(item, Mapping):
                name = item.get("name")
            else:
                name = item
            if isinstance(name, str) and name.strip():
                resources.add(name.strip())

    paths = payload.get("paths")
    if isinstance(paths, Mapping):
        for path_value in paths.values():
            if not isinstance(path_value, Mapping):
                continue
            for method, operation in path_value.items():
                if method.lower() not in HTTP_METHODS:
                    continue
                if isinstance(operation, Mapping):
                    op_tags = operation.get("tags")
                    if isinstance(op_tags, Sequence):
                        for tag in op_tags:
                            if isinstance(tag, str) and tag.strip():
                                resources.add(tag.strip())

    return resources


def _extract_resources_from_asyncapi(payload: Mapping[str, object]) -> Set[str]:
    resources: Set[str] = set()

    channels = payload.get("channels")
    if isinstance(channels, Mapping):
        for channel in channels.values():
            if not isinstance(channel, Mapping):
                continue
            for direction in ("publish", "subscribe"):
                operation = channel.get(direction)
                if not isinstance(operation, Mapping):
                    continue
                tags = operation.get("tags")
                if isinstance(tags, Sequence):
                    for tag in tags:
                        if isinstance(tag, Mapping):
                            name = tag.get("name")
                        else:
                            name = tag
                        if isinstance(name, str) and name.strip():
                            resources.add(name.strip())

                summary = operation.get("summary")
                if isinstance(summary, str):
                    words = re.split(r"[^0-9A-Za-z]+", summary)
                    if words:
                        resources.add(words[0])

    return resources


def discover_api_documents(
    source_root: Path,
    versions: Sequence[str],
) -> Sequence[APIDocument]:
    if not source_root.exists():
        return []

    documents: list[APIDocument] = []
    for version in versions:
        version_root = source_root / version
        if not version_root.exists():
            continue

        for protocol in ("openapi", "asyncapi"):
            proto_root = version_root / protocol
            if not proto_root.exists():
                continue

            for file_path in sorted(proto_root.rglob("*")):
                if not file_path.suffix.lower() in {".json", ".yaml", ".yml"}:
                    continue

                payload = _load_api_payload(file_path)
                if not payload:
                    continue

                if protocol == "openapi" and "openapi" not in payload:
                    continue
                if protocol == "asyncapi" and "asyncapi" not in payload:
                    continue

                if protocol == "openapi":
                    resources = _extract_resources_from_openapi(payload)
                else:
                    resources = _extract_resources_from_asyncapi(payload)

                if not resources:
                    continue

                documents.append(
                    APIDocument(
                        path=file_path,
                        version=version,
                        protocol=protocol,
                        name=file_path.stem,
                        resources=sorted(resources),
                        title=(
                            payload.get("info", {}).get("title")
                            if isinstance(payload.get("info"), Mapping)
                            else None
                        ),
                    )
                )

    return documents


def _iter_properties(
    node: Mapping[str, object],
    repository: "SchemaRepository",
    include_inheritance: bool = True,
    _visited_nodes: Optional[Set[int]] = None,
    _visited_schemas: Optional[Set[str]] = None,
) -> Iterable[Tuple[str, Mapping[str, object]]]:
    """遍历节点内的属性声明，自动展开 allOf/anyOf/oneOf 以及父类引用。"""

    if _visited_nodes is None:
        _visited_nodes = set()
    if _visited_schemas is None:
        _visited_schemas = set()

    node_id = id(node)
    if node_id in _visited_nodes:
        return
    _visited_nodes.add(node_id)

    properties = node.get("properties")
    if isinstance(properties, Mapping):
        for prop_name, prop_value in properties.items():
            if isinstance(prop_value, Mapping):
                yield prop_name, prop_value

    for keyword in ("allOf", "anyOf", "oneOf"):
        compositions = node.get(keyword)
        if not isinstance(compositions, Sequence):
            continue

        for item in compositions:
            if not isinstance(item, Mapping):
                continue

            ref_name = _extract_ref_from_mapping(item)
            if ref_name and include_inheritance:
                if ref_name not in _visited_schemas:
                    _visited_schemas.add(ref_name)
                    parent_record = repository.get(ref_name)
                    if parent_record:
                        parent_def = parent_record.definition()
                        if parent_def:
                            yield from _iter_properties(
                                parent_def,
                                repository,
                                include_inheritance,
                                _visited_nodes,
                                _visited_schemas,
                            )

            # 若组合节点同时带有结构字段，也继续遍历其余部分
            if any(
                key in item for key in ("properties", "allOf", "anyOf", "oneOf")
            ):
                nested = {
                    k: v for k, v in item.items() if not (k == "$ref" and isinstance(v, str))
                }
                if nested:
                    yield from _iter_properties(
                        nested,
                        repository,
                        include_inheritance,
                        _visited_nodes,
                        _visited_schemas,
                    )
            elif not ref_name:
                yield from _iter_properties(
                    item,
                    repository,
                    include_inheritance,
                    _visited_nodes,
                    _visited_schemas,
                )


def _ordered_property_entries(
    definition: Mapping[str, object],
    repository: "SchemaRepository",
    include_inheritance: bool,
) -> Sequence[Tuple[str, Mapping[str, object]]]:
    """按“自身属性→继承属性”顺序返回属性列表。"""

    direct_entries = list(
        _iter_properties(
            definition,
            repository,
            include_inheritance=False,
        )
    )

    inherited_entries: Sequence[Tuple[str, Mapping[str, object]]] = ()
    if include_inheritance:
        inherited_entries = list(
            _iter_properties(
                definition,
                repository,
                include_inheritance=True,
            )
        )

    ordered: list[Tuple[str, Mapping[str, object]]] = []
    seen: Set[str] = set()

    for prop_name, prop_value in direct_entries:
        if prop_name in seen:
            continue
        seen.add(prop_name)
        ordered.append((prop_name, prop_value))

    for prop_name, prop_value in inherited_entries:
        if prop_name in seen:
            continue
        seen.add(prop_name)
        ordered.append((prop_name, prop_value))

    return ordered


def _collect_enum_values(
    node: Mapping[str, object],
    repository: "SchemaRepository",
    include_inheritance: bool = True,
    _visited_nodes: Optional[Set[int]] = None,
    _visited_schemas: Optional[Set[str]] = None,
) -> Iterable[str]:
    """遍历节点与父类，收集所有枚举取值。"""

    if _visited_nodes is None:
        _visited_nodes = set()
    if _visited_schemas is None:
        _visited_schemas = set()

    node_id = id(node)
    if node_id in _visited_nodes:
        return
    _visited_nodes.add(node_id)

    enum_values = node.get("enum")
    if isinstance(enum_values, Sequence):
        for value in enum_values:
            if isinstance(value, (str, int, float)):
                yield str(value)

    const_value = node.get("const")
    if isinstance(const_value, (str, int, float)):
        yield str(const_value)

    for keyword in ("allOf", "anyOf", "oneOf"):
        compositions = node.get(keyword)
        if not isinstance(compositions, Sequence):
            continue

        for item in compositions:
            if not isinstance(item, Mapping):
                continue

            ref_name = _extract_ref_from_mapping(item)
            if ref_name and include_inheritance and ref_name not in _visited_schemas:
                _visited_schemas.add(ref_name)
                parent_record = repository.get(ref_name)
                if parent_record:
                    parent_def = parent_record.definition()
                    if parent_def:
                        yield from _collect_enum_values(
                            parent_def,
                            repository,
                            include_inheritance,
                            _visited_nodes,
                            _visited_schemas,
                        )

            nested = {
                k: v for k, v in item.items() if not (k == "$ref" and isinstance(v, str))
            }
            if nested:
                yield from _collect_enum_values(
                    nested,
                    repository,
                    include_inheritance,
                    _visited_nodes,
                    _visited_schemas,
                )


def _is_relationship(prop: Mapping[str, object]) -> bool:
    if "$ref" in prop:
        return True
    if prop.get("type") == "array":
        items = prop.get("items")
        if isinstance(items, Mapping) and ("$ref" in items or "allOf" in items):
            return True
    return False


def _extract_ref_from_mapping(prop: Mapping[str, object]) -> Optional[str]:
    """提取属性中引用的目标 Schema 名称。"""

    ref_value: Optional[str] = None

    if prop.get("type") == "array":
        items = prop.get("items")
        if isinstance(items, Mapping):
            if "$ref" in items:
                ref_value = items.get("$ref")  # type: ignore[assignment]
            elif isinstance(items.get("allOf"), list):
                for candidate in items["allOf"]:  # type: ignore[index]
                    if isinstance(candidate, Mapping) and "$ref" in candidate:
                        ref_value = candidate["$ref"]  # type: ignore[index]
                        break
    elif "$ref" in prop:
        ref_value = prop.get("$ref")  # type: ignore[assignment]

    if not isinstance(ref_value, str):
        return None

    # 处理形如 '../Common/Entity.schema.json#Entity' 的引用。
    if "#" in ref_value:
        schema_name = ref_value.split("#")[-1]
    else:
        schema_name = Path(ref_value).stem.replace(".schema", "")

    return schema_name or None


def _describe_property_type(
    prop: Mapping[str, object],
    repository: SchemaRepository,
    primary_language: Optional[str],
    fallback_language: Optional[str],
    bilingual: bool,
) -> str:
    """生成属性类型的可读描述，支持引用与数组。"""

    if prop.get("type") == "array":
        items = prop.get("items")
        if isinstance(items, Mapping):
            ref_name = _extract_ref_from_mapping(items)
            if ref_name:
                target = repository.get(ref_name)
                if target:
                    label = target.localized_title(
                        primary_language,
                        fallback_language,
                        bilingual,
                    )
                else:
                    label = ref_name
                return f"→ {label}[]"

            item_type = items.get("type")
            if isinstance(item_type, str):
                return f"[{item_type}]"
            if isinstance(item_type, list) and item_type:
                joined = "/".join(str(value) for value in item_type)
                return f"[{joined}]"

        return "array"

    ref_name = _extract_ref_from_mapping(prop)
    if ref_name:
        target = repository.get(ref_name)
        if target:
            label = target.localized_title(
                primary_language,
                fallback_language,
                bilingual,
            )
        else:
            label = ref_name
        return f"→ {label}"

    prop_type = prop.get("type", "any")
    if isinstance(prop_type, str):
        return prop_type
    if isinstance(prop_type, list) and prop_type:
        return "/".join(str(item) for item in prop_type)

    return "any"


def render_entity_block(
    record: SchemaRecord,
    repository: SchemaRepository,
    display_name: str,
    primary_language: Optional[str],
    fallback_language: Optional[str],
    bilingual: bool,
    include_inheritance: bool,
) -> str:
    definition = record.definition()
    if not definition:
        return (
            f'entity "{display_name}" as Missing_{record.name} #red {{\n'
            "  .. 未能解析 definition ..\n}\n"
        )

    lines = [f'entity "{display_name}" as {record.domain}_{record.name} {{']
    for prop_name, prop_value in _ordered_property_entries(
        definition, repository, include_inheritance
    ):
        display_type = _describe_property_type(
            prop_value,
            repository,
            primary_language,
            fallback_language,
            bilingual,
        )
        prop_label = record.property_display_name(
            repository,
            prop_name,
            primary_language,
            fallback_language,
            bilingual,
        )
        lines.append(f"  + {prop_label}: {display_type}")

    enum_values = list(
        dict.fromkeys(
            _collect_enum_values(
                definition,
                repository,
                include_inheritance,
            )
        )
    )
    if enum_values:
        lines.append("  .. 枚举 ..")
        for value in enum_values:
            lines.append(f"  # {value}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def discover_relationships(
    record: SchemaRecord,
    repository: SchemaRepository,
    include_inheritance: bool,
) -> Tuple[Set[Tuple[str, str, bool, str]], Set[str]]:
    definition = record.definition()
    if not definition:
        return set(), set()

    relationships: Set[Tuple[str, str, bool, str]] = set()
    discovered: Set[str] = set()

    for prop_name, prop_value in _ordered_property_entries(
        definition, repository, include_inheritance
    ):
        ref_name = _extract_ref_from_mapping(prop_value)
        if not ref_name:
            continue

        discovered.add(ref_name)

        if prop_value.get("type") == "array":
            is_many = True
        else:
            is_many = False

        relationships.add((record.name, ref_name, is_many, prop_name))

    return relationships, discovered


def discover_inheritance(
    record: SchemaRecord,
) -> Set[str]:
    definition = record.definition()
    if not definition:
        return set()

    parents: Set[str] = set()
    visited: Set[int] = set()

    def _walk(node: Mapping[str, object]) -> None:
        node_id = id(node)
        if node_id in visited:
            return
        visited.add(node_id)

        for keyword in ("allOf", "anyOf", "oneOf"):
            compositions = node.get(keyword)
            if not isinstance(compositions, Sequence):
                continue
            for item in compositions:
                if not isinstance(item, Mapping):
                    continue
                ref_name = _extract_ref_from_mapping(item)
                if ref_name:
                    has_structural_keys = any(
                        key in item for key in ("properties", "items")
                    )
                    if not has_structural_keys:
                        parents.add(ref_name)
                        continue
                _walk(item)

    _walk(definition)
    return parents


def generate_diagram(
    repository: SchemaRepository,
    start_entities: Sequence[str],
    depth: int,
    primary_language: Optional[str],
    fallback_language: Optional[str],
    bilingual: bool,
    inheritance_overrides: Mapping[str, Mapping[str, Set[str]]],
    enable_inheritance: bool,
    inheritance_scope: Optional[Set[str]] = None,
) -> str:
    queue: deque[Tuple[str, int]] = deque((entity, 0) for entity in start_entities)
    processed: Set[str] = set()
    entity_blocks: Dict[str, str] = {}
    relationship_lines: Set[str] = set()

    relationship_depth_limit = max(depth - 1, 0)

    while queue:
        entity_name, current_depth = queue.popleft()
        if entity_name in processed or current_depth > depth:
            continue

        processed.add(entity_name)
        record = repository.get(entity_name)

        if record is None:
            placeholder = f'entity "{entity_name}" as Missing_{entity_name} #red {{\n  .. 未在仓库中找到 ..\n}}\n'
            entity_blocks[entity_name] = placeholder
            continue

        display_name = record.localized_title(
            primary_language, fallback_language, bilingual
        )
        include_inheritance = enable_inheritance and (
            inheritance_scope is None or entity_name in inheritance_scope
        )

        entity_blocks[entity_name] = render_entity_block(
            record,
            repository,
            display_name,
            primary_language,
            fallback_language,
            bilingual,
            include_inheritance,
        )

        relationships, discovered = discover_relationships(
            record,
            repository,
            include_inheritance,
        )
        if current_depth <= relationship_depth_limit:
            for source, target, is_many, prop_name in relationships:
                source_record = repository.get(source)
                target_record = repository.get(target)

                source_alias = (
                    f"{source_record.domain}_{source_record.name}"
                    if source_record
                    else source
                )
                target_alias = (
                    f"{target_record.domain}_{target_record.name}"
                    if target_record
                    else target
                )
                prop_label = record.property_display_name(
                    repository,
                    prop_name,
                    primary_language,
                    fallback_language,
                    bilingual,
                )

                left_card = '"1"'
                right_card = '"0..*"' if is_many else '"0..1"'
                connector = "--{" if is_many else "--"

                relationship_lines.add(
                    f"{source_alias} {left_card} {connector} {right_card} {target_alias} : {prop_label}"
                )

        inheritance_parents: Set[str] = set()
        if include_inheritance:
            inheritance_parents = discover_inheritance(record)
            override_key = _normalize_name(record.name)
            config = inheritance_overrides.get(override_key)
            if config:
                if "replace" in config:
                    inheritance_parents = set(config["replace"])
                else:
                    inheritance_parents.update(config.get("add", set()))
                for removed in config.get("remove", set()):
                    inheritance_parents.discard(removed)

        if inheritance_parents:
            for parent in sorted(inheritance_parents):
                parent_record = repository.get(parent)
                parent_label = (
                    parent_record.localized_title(
                        primary_language, fallback_language, bilingual
                    )
                    if parent_record
                    else parent
                )
                parent_alias = (
                    f"{parent_record.domain}_{parent_record.name}"
                    if parent_record
                    else parent
                )
                relationship_lines.add(
                    f"{parent_alias} <|-- {record.domain}_{record.name}"
                )
            discovered.update(inheritance_parents)

        if current_depth < depth:
            for item in sorted(discovered):
                if item not in processed:
                    queue.append((item, current_depth + 1))

    header = "@startuml\n!theme vibrant\nskinparam shadowing true\n"
    footer = "\n@enduml\n"
    entity_section = "\n".join(entity_blocks[entity] for entity in sorted(entity_blocks.keys()))
    relationship_section = "\n".join(sorted(relationship_lines))

    return f"{header}\n' --- Entities ---\n{entity_section}\n' --- Relationships ---\n{relationship_section}\n{footer}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="根据 Schema 仓库生成 PlantUML ER 图")
    parser.add_argument(
        "-r",
        "--resources",
        nargs="+",
        help="一个或多个起始实体名称，缺省时使用 Product / Service / Customer",
    )
    parser.add_argument(
        "-d",
        "--depth",
        type=int,
        default=DEFAULT_MAX_DEPTH,
        help=f"关系递归深度，默认为 {DEFAULT_MAX_DEPTH}",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="输出 PlantUML 文件路径，缺省时打印到控制台",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        help="Schema 仓库根目录，默认优先选择 dist/json，其次 dist/validation",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=DEFAULT_SOURCE_ROOT,
        help="官方 API 文档根目录，默认 sources/tmf-official",
    )
    parser.add_argument(
        "--i18n-root",
        type=Path,
        default=DEFAULT_I18N_ROOT,
        help="翻译资源目录，默认读取 overrides/i18n 以加载 CSV/工作簿翻译",
    )
    parser.add_argument(
        "--versions",
        nargs="+",
        default=list(DEFAULT_VERSIONS),
        help="参与资源提取的主版本号目录，默认仅处理 API-v4 与 API-v5",
    )
    parser.add_argument(
        "--api",
        help="指定某份 API 文档（文件名或标题关键字），自动使用其资源入口作为起点",
    )
    parser.add_argument(
        "--label-language",
        help="优先展示的语言代码，未指定时由语言模式决定",
    )
    parser.add_argument(
        "--fallback-language",
        help="次选语言代码",
    )
    parser.add_argument(
        "--bilingual",
        action="store_true",
        help="启用双语展示（主语言 + 回退语言）",
    )
    parser.add_argument(
        "--language-mode",
        choices=("zh", "en", "both"),
        help="快速切换中文、英文或双语视图（优先于单独的语言参数）",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="仅列出仓库中的可用实体名称并退出",
    )
    parser.add_argument(
        "--list-apis",
        action="store_true",
        help="列出可解析的 API 文档及资源入口并退出",
    )
    parser.add_argument(
        "--dump-resource-index",
        type=str,
        default="dist/docs/api_resources.yaml",
        help="将 API 端点资源导出为索引文件，设置为 '-' 可跳过写入",
    )
    parser.add_argument(
        "--emit-version-diagrams",
        type=Path,
        help="按版本批量生成默认 ER 图输出目录，例如 dist/puml",
    )
    parser.add_argument(
        "--inheritance-config",
        type=Path,
        help="自定义继承关系配置文件（YAML/JSON），可覆盖或新增父类",
    )
    parser.add_argument(
        "--no-inheritance",
        action="store_true",
        help="禁用自动识别继承关系，仅展示属性关联",
    )
    parser.add_argument(
        "--inheritance-scope",
        choices=("start", "all"),
        default="start",
        help="控制继承展开范围：start=仅针对起始实体，all=对所有实体展开",
    )
    return parser.parse_args()


def _resolve_repository(args: argparse.Namespace) -> Path:
    if args.repo:
        return args.repo

    for candidate in DEFAULT_REPO_CANDIDATES:
        if any(candidate.rglob("*.schema.json")):
            return candidate

    # 回退到根目录下的业务域文件夹
    for domain in ("BusinessPartner", "Customer", "Product", "Service", "Resource", "Common"):
        domain_path = Path(domain)
        if domain_path.exists() and any(domain_path.glob("*.schema.json")):
            return Path(".")

    return DEFAULT_REPO_CANDIDATES[0]


def _collect_default_from_documents(
    documents: Sequence[APIDocument], repository: SchemaRepository, limit: int = 6
) -> Sequence[str]:
    collected: list[str] = []
    for doc in documents:
        for resource in doc.resolved_resources(repository):
            if resource not in collected:
                collected.append(resource)
            if len(collected) >= limit:
                return collected
    return collected


def _select_start_entities(
    repository: SchemaRepository,
    requested: Optional[Sequence[str]],
    chosen_document: Optional[APIDocument],
    all_documents: Sequence[APIDocument],
) -> Sequence[str]:
    if requested:
        return list(requested)

    if chosen_document:
        resolved = chosen_document.resolved_resources(repository)
        if resolved:
            return resolved

    resolved_from_docs = _collect_default_from_documents(all_documents, repository)
    if resolved_from_docs:
        return resolved_from_docs

    available = [name for name in DEFAULT_CORE_ENTITIES if repository.contains(name)]
    if available:
        return available

    # 若默认核心实体不存在，则取前几个按字母排序的实体名称。
    collected: Set[str] = set()
    alias_map = repository.list_aliases()
    for aliases in alias_map.values():
        collected.update(aliases)
    if not collected:
        return DEFAULT_CORE_ENTITIES

    return sorted(collected)[:5]


def _match_api_document(documents: Sequence[APIDocument], keyword: str) -> Optional[APIDocument]:
    normalized = keyword.strip().lower()
    if not normalized:
        return None

    # 先尝试文件名精确匹配
    for doc in documents:
        if doc.path.name.lower() == normalized:
            return doc
        if doc.name.lower() == normalized:
            return doc

    # 再尝试包含关键字
    for doc in documents:
        if normalized in doc.path.name.lower() or normalized in doc.name.lower():
            return doc
        if doc.title and normalized in doc.title.lower():
            return doc

    return None


def _print_entity_list(repository: SchemaRepository, primary_language: str, fallback_language: str, bilingual: bool) -> None:
    print("📚 可用实体清单：")
    alias_map = repository.list_aliases()
    for domain in sorted(alias_map.keys()):
        print(f"- {domain}:")
        names = sorted(alias_map[domain])
        for name in names:
            record = repository.get(name, domain)
            if not record:
                continue
            display = record.localized_title(primary_language, fallback_language, bilingual)
            if display != name:
                print(f"    · {name} → {display}")
            else:
                print(f"    · {name}")


def _print_api_documents(documents: Sequence[APIDocument], repository: SchemaRepository) -> None:
    if not documents:
        print("⚠️  未在指定目录中解析到 API 文档，请确认 sources/tmf-official 已同步官方资产。")
        return

    print("📄 可用 API 文档：")
    for doc in documents:
        resolved = doc.resolved_resources(repository)
        resources = ", ".join(resolved) if resolved else "<未在仓库中找到匹配实体>"
        title = f"（{doc.title}）" if doc.title else ""
        print(
            f"- {doc.version}/{doc.protocol} :: {doc.path.name}{title}\n  ↳ 资源入口：{resources or '暂无'}"
        )


def _relative_to(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def export_resource_index(
    documents: Sequence[APIDocument],
    repository: SchemaRepository,
    destination: Path,
    source_root: Path,
) -> None:
    if not documents:
        print("⚠️  未找到可导出的 API 文档，跳过资源索引写入。")
        return

    grouped: Dict[str, list[dict]] = {}
    for doc in documents:
        resolved = doc.resolved_resources(repository)
        entry = {
            "file": _relative_to(doc.path, source_root),
            "protocol": doc.protocol,
            "title": doc.title,
            "resources": resolved or list(doc.resources),
        }
        grouped.setdefault(doc.version, []).append(entry)

    for items in grouped.values():
        items.sort(key=lambda item: item["file"])

    payload = {"versions": grouped}
    destination.parent.mkdir(parents=True, exist_ok=True)

    if yaml is not None:
        with destination.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=True)
    else:
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(f"✅ 已导出 API 资源索引：{destination}")


def _ensure_string_set(value: object) -> Set[str]:
    result: Set[str] = set()
    if isinstance(value, str) and value.strip():
        result.add(value.strip())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            if isinstance(item, str) and item.strip():
                result.add(item.strip())
    return result


def load_inheritance_overrides(
    config_path: Optional[Path],
) -> Dict[str, Dict[str, Set[str]]]:
    overrides: Dict[str, Dict[str, Set[str]]] = {}
    if not config_path:
        return overrides

    if not config_path.exists():
        print(f"⚠️  未找到继承配置文件：{config_path}")
        return overrides

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - I/O 提示
        print(f"⚠️  无法读取继承配置 {config_path}: {exc}")
        return overrides

    if not text.strip():
        return overrides

    try:
        if config_path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            if yaml is None:
                print("⚠️  当前环境未安装 PyYAML，无法解析 YAML 格式的继承配置。")
                return overrides
            data = yaml.safe_load(text)  # type: ignore[assignment]
    except (json.JSONDecodeError, yaml.YAMLError) as exc:  # type: ignore[attr-defined]
        print(f"⚠️  解析继承配置失败 {config_path}: {exc}")
        return overrides

    if not isinstance(data, Mapping):
        return overrides

    for child_name, payload in data.items():
        if not isinstance(child_name, str) or not child_name.strip():
            continue
        normalized = _normalize_name(child_name)

        replace: Set[str] = set()
        add: Set[str] = set()
        remove: Set[str] = set()

        if isinstance(payload, Mapping):
            replace = _ensure_string_set(payload.get("replace"))
            add = _ensure_string_set(payload.get("add"))
            remove = _ensure_string_set(payload.get("remove"))
        elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
            replace = _ensure_string_set(payload)
        elif isinstance(payload, str) and payload.strip():
            replace = {payload.strip()}
        else:
            continue

        entry: Dict[str, Set[str]] = {}
        if replace:
            entry["replace"] = replace
        if add:
            entry["add"] = add
        if remove:
            entry["remove"] = remove

        if entry:
            overrides[normalized] = entry

    return overrides


def emit_version_diagrams(
    documents: Sequence[APIDocument],
    repository: SchemaRepository,
    output_root: Path,
    depth: int,
    primary_language: str,
    fallback_language: Optional[str],
    bilingual: bool,
    inheritance_overrides: Mapping[str, Mapping[str, Set[str]]],
    enable_inheritance: bool,
    inheritance_scope_mode: str,
) -> None:
    if not documents:
        print("⚠️  未找到可用于生成 ER 图的 API 文档。")
        return

    grouped: Dict[str, list[APIDocument]] = {}
    for doc in documents:
        grouped.setdefault(doc.version, []).append(doc)

    for version, docs in grouped.items():
        version_dir = output_root / version
        version_dir.mkdir(parents=True, exist_ok=True)

        for doc in sorted(docs, key=lambda item: item.path.stem):
            start_entities = doc.resolved_resources(repository)
            if not start_entities:
                start_entities = list(doc.resources)
            if not start_entities:
                print(
                    f"⚠️  {doc.path.name} 未识别到资源入口，已跳过对应图谱生成。",
                )
                continue

            scope = None
            if enable_inheritance and inheritance_scope_mode == "start":
                scope = set(start_entities)

            diagram = generate_diagram(
                repository,
                start_entities,
                depth,
                primary_language,
                fallback_language,
                bilingual,
                inheritance_overrides,
                enable_inheritance,
                scope,
            )

            output_path = version_dir / f"{doc.path.stem}.puml"
            output_path.write_text(diagram, encoding="utf-8")
            print(
                f"✅ 已生成 {version} / {doc.path.name} 的 ER 图：{output_path}"
            )

def main() -> None:
    args = parse_args()
    repo_root = _resolve_repository(args)
    repository = load_repository(repo_root, args.i18n_root)

    api_documents = discover_api_documents(args.source_root, args.versions)
    chosen_document = _match_api_document(api_documents, args.api) if args.api else None
    if args.api and not chosen_document:
        print(f"⚠️  未找到与 '{args.api}' 匹配的 API 文档，已回退到通用实体列表。")

    def _resolve_languages() -> Tuple[str, Optional[str], bool]:
        primary = args.label_language
        fallback = args.fallback_language
        bilingual = args.bilingual

        if args.language_mode == "zh":
            primary = primary or "zh-CN"
            fallback = fallback or "en"
            bilingual = False
        elif args.language_mode == "en":
            primary = primary or "en"
            fallback = fallback or "zh-CN"
            bilingual = False
        elif args.language_mode == "both":
            primary = primary or "zh-CN"
            fallback = fallback or "en"
            bilingual = True
        else:
            if not primary:
                primary = "zh-CN"
            if fallback is None and primary and primary.lower() != "en":
                fallback = "en"

        return primary, fallback, bilingual

    primary_language, fallback_language, bilingual = _resolve_languages()

    inheritance_overrides = load_inheritance_overrides(args.inheritance_config)
    inheritance_enabled = not args.no_inheritance
    if not inheritance_enabled:
        inheritance_overrides = {}

    dump_target = args.dump_resource_index
    resource_index_path = None if dump_target == "-" else Path(dump_target)
    if resource_index_path:
        export_resource_index(
            api_documents,
            repository,
            resource_index_path,
            args.source_root,
        )

    if args.emit_version_diagrams:
        emit_version_diagrams(
            api_documents,
            repository,
            args.emit_version_diagrams,
            max(args.depth, 0),
            primary_language,
            fallback_language,
            bilingual,
            inheritance_overrides,
            inheritance_enabled,
            args.inheritance_scope,
        )

    if args.list:
        _print_entity_list(
            repository,
            primary_language,
            fallback_language,
            bilingual,
        )
        return

    if args.list_apis:
        _print_api_documents(api_documents, repository)
        return

    start_entities = _select_start_entities(
        repository,
        args.resources,
        chosen_document,
        api_documents,
    )

    if chosen_document:
        print(
            f"🌱 正在基于 {chosen_document.path.name} 的资源入口生成图谱：{', '.join(start_entities)}，探索深度 {args.depth} 层。"
        )
    else:
        print(
            f"🌱 正在以 {', '.join(start_entities)} 为起点生成图谱，探索深度 {args.depth} 层。"
        )

    inheritance_scope = None
    if inheritance_enabled and args.inheritance_scope == "start":
        inheritance_scope = set(start_entities)

    diagram = generate_diagram(
        repository,
        start_entities,
        max(args.depth, 0),
        primary_language,
        fallback_language,
        bilingual,
        inheritance_overrides,
        inheritance_enabled,
        inheritance_scope,
    )

    if args.output:
        args.output.write_text(diagram, encoding="utf-8")
        print(f"✅ 已输出 PlantUML 文件：{args.output}")
    else:
        print("\n--- PlantUML ---\n")
        print(diagram)


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    main()
