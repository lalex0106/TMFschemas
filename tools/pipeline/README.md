# 官方 YAML 到企业 Schema 流水线（原型）

本目录提供一个基于 Python 的最小可运行原型，用于演示如何将官方发布的 Open API/AsyncAPI 规范
（无论是 YAML 还是 JSON）转换为企业化管理的 JSON Schema 资产。脚本吸收了用户提供的本地脚本优点，强化了以下特性：

- **配置驱动**：全部路径、阈值与元数据通过 `pipeline.config.yaml` 管理，便于在不同环境中复用。
- **阶段清晰**：保持“分析映射 -> 生成重写”两阶段设计，先确定域归属，再重写 `$ref`。
- **渐进落地**：输出目录与源目录均已在仓库中占位，可逐步补齐官方与企业扩展的原始文件。
- **容错日志**：解析失败时输出告警，不阻塞整体流程，便于后续排查质量问题。
- **多协议优先级**：同一模型若在 OpenAPI 与 AsyncAPI 中同时出现，将自动优先采用 REST 定义，保持输出一致性。
- **元数据沉淀**：生成的 Schema 将携带来源 API、版本、协议与使用频次等元数据，便于后续治理。
- **多语言融合**：读取 `overrides/i18n/` 中的翻译文件，自动生成中英文对照的 `x-i18n` 字段，兼顾国际化与本地化需求。

## 使用步骤

1. 将官方规范复制或同步至 `sources/tmf-official/API-v*/` 对应版本目录：
   - `openapi/` 子目录放置 `*.oas.yaml` 等 REST 定义。
   - `asyncapi/` 子目录放置 `*.asyncapi.json`/`*.asyncapi.yaml` 等事件接口。
2. 如有额外参考源，可放入 `sources/external/`。
3. 根据需要编辑 `config/domain_mapping.yaml` 与 `config/name_mapping.json`，统一域映射与命名规范。
4. 可选：在 `overrides/i18n/<locale>/` 目录补充翻译文件（详见 `overrides/i18n/README.md`）。
   - 若 `Schemas_ZH.csv` 提供模型标题与描述的中文翻译，脚本会在输出中自动写入 `x-i18n`。
   - `Properties_ZH.csv` 支持仅使用 `Property,Descriptions,中文名称,新描述` 四列表头，流水线会把同名属性翻译同步到所有出现该属性的节点；
     如需覆盖单个模型，可增加 `Schema` 列或在 `Property` 列使用 `模型.属性` 的写法（如 `Account.description`），以避免全局配置覆盖差异化语义。
5. （首次执行前）在 Python 环境中安装依赖：

   ```bash
   pip install pyyaml
   ```

6. 执行：

   ```bash
   python tools/pipeline/build_schemas.py --clean
   ```

7. 生成的企业级 schema 将按照域分类输出到 `dist/json/`，若提供了翻译文件，会在 `x-i18n` 中展示可用语种列表；同时会在 `dist/validation/` 目录生成自动剥离 `x-metadata`、`x-i18n` 等扩展字段的严格版本，便于通过 TMF 官方校验。

## 翻译配置常见问答

- **为何属性的 `x-i18n` 没有出现？**
  - 请确认 CSV 至少包含 `Property` 列；如仅保留四列表头，也需要填写属性名称，流水线会自动把翻译应用到所有匹配的属性节点。
  - 若属性需要限定到某个模型，请增加 `Schema` 列或使用 `模型.属性` 的写法（例如 `Account.description`）。
  - 多个文件提供同一属性翻译时，脚本会按照“模型限定 > 全局属性 > YAML/JSON”顺序合并，保证精确覆盖且不会被全局配置覆盖。
- **发现属性含义在不同模型中不一致怎么办？**
  - 在 `Properties_ZH.csv` 中为该属性新增一行，指定 `Schema` 或使用 `模型.属性` 写法，为特定模型提供差异化翻译；流水线会优先使用更具体的配置。
  - 或者在 `overrides/i18n/<locale>/` 下补充同名 YAML/JSON 覆盖文件，脚本会自动合并并以文件内的内容为准。

## 校验生成结果是否符合 TMF 规范

官方仓库附带了基于 JSON Schema Meta-Schema 的校验脚本，本仓库封装了便捷入口：

1. 安装 Node 依赖：

   ```bash
   npm install
   ```

2. 执行 Python 封装脚本：

   ```bash
   python tools/pipeline/run_validation.py
   ```

   默认会读取 `dist/validation/`（已剥离 `x-*` 扩展）的 Schema，并调用 `.circleci/validate.js`。校验日志同时输出在控制台与 `dist/validation/validation_results.txt` 中。

3. 若需要手动指定目录，可使用参数：`python tools/pipeline/run_validation.py --schemas dist/json`。

> 提示：日志中如仅提示 `x-metadata`、`x-i18n` 等扩展被禁止，可认定为企业定制能力；关于 `type`、`discriminator` 等基础字段的报错则代表真实规范问题，需要回溯源文件或企业自定义改动。

## 后续扩展建议

- 将 `run_pipeline` 包装为 CLI 工具，增加差异比对与指标输出。
- 在 CI 中执行，结合单元测试验证 `resolve_refs`、`infer_domain` 等关键函数。
- 与 `docs/` 中的计划文档协同，持续完善企业级元数据和多格式发布能力。
