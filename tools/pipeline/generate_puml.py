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
DEFAULT_VERSIONS = ("API-v4", "API-v5")
DEFAULT_CORE_ENTITIES = ("Product", "Service", "Customer")
DEFAULT_MAX_DEPTH = 2
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def _normalize_name(value: str) -> str:
    """将名称统一为大小写不敏感且忽略分隔符的索引键。"""

    compact = re.sub(r"[\s_\-]+", "", value)
    return compact.lower()


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

    def localized_title(
        self,
        primary_language: Optional[str],
        fallback_language: Optional[str],
        bilingual: bool,
    ) -> str:
        """根据语言偏好生成展示名称。"""

        base_name = self.title or self.name

        def _pick(language: Optional[str]) -> Optional[str]:
            if not language:
                return None
            locale = self.translations.get(language)
            if isinstance(locale, Mapping):
                candidate = locale.get("title")
                if isinstance(candidate, str) and candidate:
                    return candidate
            if language.lower().startswith("en"):
                return base_name
            return None

        primary = _pick(primary_language)
        secondary = _pick(fallback_language) if fallback_language else None

        if bilingual:
            chosen_primary = primary or base_name
            chosen_secondary = secondary or (base_name if chosen_primary != base_name else self.name)
            if chosen_primary == chosen_secondary:
                return chosen_primary
            return f"{chosen_primary} ({chosen_secondary})"

        return primary or secondary or base_name


class SchemaRepository:
    """帮助在内存中索引全部 Schema。"""

    def __init__(self) -> None:
        self._records: Dict[Tuple[str, str], SchemaRecord] = {}
        self._aliases: Dict[str, Set[Tuple[str, str]]] = {}

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


def load_repository(repo_root: Path) -> SchemaRepository:
    if not repo_root.exists():
        raise SystemExit(f"❌ 未找到 Schema 仓库目录：{repo_root}")

    repository = SchemaRepository()
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


def _iter_properties(node: Mapping[str, object]) -> Iterable[Tuple[str, Mapping[str, object]]]:
    properties = node.get("properties")
    if isinstance(properties, Mapping):
        for prop_name, prop_value in properties.items():
            if isinstance(prop_value, Mapping):
                yield prop_name, prop_value


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


def render_entity_block(record: SchemaRecord) -> str:
    definition = record.definition()
    if not definition:
        return f'entity "{record.name}" as Missing_{record.name} #red {{\n  .. 未能解析 definition ..\n}}\n'

    lines = [f'entity "{record.name}" as {record.domain}_{record.name} {{']
    for prop_name, prop_value in _iter_properties(definition):
        if _is_relationship(prop_value):
            continue
        prop_type = prop_value.get("type", "any")
        if isinstance(prop_type, str):
            display_type = prop_type
        elif isinstance(prop_type, list):
            display_type = "/".join(str(item) for item in prop_type)
        else:
            display_type = "any"
        lines.append(f"  + {prop_name}: {display_type}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def discover_relationships(
    record: SchemaRecord, repository: SchemaRepository
) -> Tuple[Set[Tuple[str, str, bool, str]], Set[str]]:
    definition = record.definition()
    if not definition:
        return set(), set()

    relationships: Set[Tuple[str, str, bool, str]] = set()
    discovered: Set[str] = set()

    for prop_name, prop_value in _iter_properties(definition):
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


def generate_diagram(
    repository: SchemaRepository,
    start_entities: Sequence[str],
    depth: int,
    primary_language: Optional[str],
    fallback_language: Optional[str],
    bilingual: bool,
) -> str:
    queue: deque[Tuple[str, int]] = deque((entity, 0) for entity in start_entities)
    processed: Set[str] = set()
    entity_blocks: Dict[str, str] = {}
    relationship_lines: Set[str] = set()

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

        display_name = record.localized_title(primary_language, fallback_language, bilingual)
        entity_blocks[entity_name] = render_entity_block(record).replace(
            f'entity "{record.name}"', f'entity "{display_name}"'
        )

        relationships, discovered = discover_relationships(record, repository)
        for source, target, is_many, prop_name in relationships:
            source_record = repository.get(source)
            target_record = repository.get(target)

            source_label = (
                source_record.localized_title(primary_language, fallback_language, bilingual)
                if source_record
                else source
            )
            target_label = (
                target_record.localized_title(primary_language, fallback_language, bilingual)
                if target_record
                else target
            )

            left_card = '"1"'
            right_card = '"0..*"' if is_many else '"0..1"'
            connector = "--{" if is_many else "--"

            relationship_lines.add(
                f'"{source_label}" {left_card} {connector} {right_card} "{target_label}" : {prop_name}'
            )

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


def main() -> None:
    args = parse_args()
    repo_root = _resolve_repository(args)
    repository = load_repository(repo_root)

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

    diagram = generate_diagram(
        repository,
        start_entities,
        max(args.depth, 0),
        primary_language,
        fallback_language,
        bilingual,
    )

    if args.output:
        args.output.write_text(diagram, encoding="utf-8")
        print(f"✅ 已输出 PlantUML 文件：{args.output}")
    else:
        print("\n--- PlantUML ---\n")
        print(diagram)


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    main()

