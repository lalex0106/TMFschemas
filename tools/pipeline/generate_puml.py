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
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Set, Tuple


DEFAULT_REPO = Path("dist/json")
DEFAULT_CORE_ENTITIES = ("Product", "Service", "Customer")
DEFAULT_MAX_DEPTH = 2


@dataclass(frozen=True)
class SchemaRecord:
    """存储单个 Schema 的元信息与内容。"""

    name: str
    domain: str
    path: Path
    payload: Mapping[str, object]

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


class SchemaRepository:
    """帮助在内存中索引全部 Schema。"""

    def __init__(self) -> None:
        self._by_domain_name: Dict[Tuple[str, str], SchemaRecord] = {}
        self._by_name: Dict[str, Set[str]] = {}

    def add(self, record: SchemaRecord) -> None:
        key = (record.domain, record.name)
        self._by_domain_name[key] = record
        self._by_name.setdefault(record.name, set()).add(record.domain)

    def get(self, name: str, domain: Optional[str] = None) -> Optional[SchemaRecord]:
        if domain:
            return self._by_domain_name.get((domain, name))

        domains = self._by_name.get(name)
        if not domains:
            return None
        if len(domains) == 1:
            only_domain = next(iter(domains))
            return self._by_domain_name.get((only_domain, name))

        # 若存在多域重名，默认返回其中一个并留给调用方处理。
        first_domain = sorted(domains)[0]
        return self._by_domain_name.get((first_domain, name))

    def __len__(self) -> int:  # pragma: no cover - 仅用于提示信息
        return len(self._by_domain_name)


def _discover_schema_name(payload: Mapping[str, object], file_path: Path) -> Optional[str]:
    definitions = payload.get("definitions")
    if isinstance(definitions, Mapping):
        for key in definitions.keys():
            if isinstance(key, str) and key:
                return key

    stem = file_path.stem.replace(".schema", "")
    return stem if stem else None


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

        repository.add(SchemaRecord(schema_name, domain, file_path, payload))

    if not len(repository):  # pragma: no cover - 空仓库提示
        raise SystemExit("❌ 未成功加载任何 Schema，目录可能为空或结构异常。")

    print(f"✅ 已加载 {len(repository)} 个 Schema 定义。")
    return repository


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


def discover_relationships(record: SchemaRecord, repository: SchemaRepository) -> Tuple[Set[str], Set[str]]:
    definition = record.definition()
    if not definition:
        return set(), set()

    relationships: Set[str] = set()
    discovered: Set[str] = set()

    for prop_name, prop_value in _iter_properties(definition):
        ref_name = _extract_ref_from_mapping(prop_value)
        if not ref_name:
            continue

        discovered.add(ref_name)

        if prop_value.get("type") == "array":
            left_card, right_card, connector = '"1"', '"0..*"', "--{"  # type: ignore[assignment]
        else:
            left_card, right_card, connector = '"1"', '"0..1"', "--"

        relationships.add(
            f'"{record.name}" {left_card} {connector} {right_card} "{ref_name}" : {prop_name}'
        )

    return relationships, discovered


def generate_diagram(
    repository: SchemaRepository,
    start_entities: Sequence[str],
    depth: int,
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

        entity_blocks[entity_name] = render_entity_block(record)
        relationships, discovered = discover_relationships(record, repository)
        relationship_lines.update(relationships)

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
    parser = argparse.ArgumentParser(description="根据 dist/json 目录生成 PlantUML ER 图")
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
        default=DEFAULT_REPO,
        help="Schema 仓库根目录，默认读取 dist/json",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repository = load_repository(args.repo)

    start_entities: Sequence[str]
    if args.resources:
        start_entities = args.resources
    else:
        start_entities = DEFAULT_CORE_ENTITIES

    print(
        f"🌱 正在以 {', '.join(start_entities)} 为起点生成图谱，探索深度 {args.depth} 层。"
    )
    diagram = generate_diagram(repository, start_entities, max(args.depth, 0))

    if args.output:
        args.output.write_text(diagram, encoding="utf-8")
        print(f"✅ 已输出 PlantUML 文件：{args.output}")
    else:
        print("\n--- PlantUML ---\n")
        print(diagram)


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    main()

