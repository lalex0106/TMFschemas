# 官方 YAML 到企业 Schema 流水线（原型）

本目录提供一个基于 Python 的最小可运行原型，用于演示如何将官方发布的 Open API/AsyncAPI 规范
（无论是 YAML 还是 JSON）转换为企业化管理的 JSON Schema 资产。脚本吸收了用户提供的本地脚本优点，强化了以下特性：

- **配置驱动**：全部路径、阈值与元数据通过 `pipeline.config.yaml` 管理，便于在不同环境中复用。
- **阶段清晰**：保持“分析映射 -> 生成重写”两阶段设计，先确定域归属，再重写 `$ref`。
- **渐进落地**：输出目录与源目录均已在仓库中占位，可逐步补齐官方与企业扩展的原始文件。
- **容错日志**：解析失败时输出告警，不阻塞整体流程，便于后续排查质量问题。
- **多协议优先级**：同一模型若在 OpenAPI 与 AsyncAPI 中同时出现，将自动优先采用 REST 定义，保持输出一致性。
- **元数据沉淀**：生成的 Schema 将携带来源 API、版本、协议与使用频次等元数据，便于后续治理。

## 使用步骤

1. 将官方规范复制或同步至 `sources/tmf-official/API-v*/` 对应版本目录：
   - `openapi/` 子目录放置 `*.oas.yaml` 等 REST 定义。
   - `asyncapi/` 子目录放置 `*.asyncapi.json`/`*.asyncapi.yaml` 等事件接口。
2. 如有额外参考源，可放入 `sources/external/`。
3. 根据需要编辑 `config/domain_mapping.yaml` 与 `config/name_mapping.json`，统一域映射与命名规范。
4. （首次执行前）在 Python 环境中安装依赖：

   ```bash
   pip install pyyaml
   ```

5. 执行：

   ```bash
   python tools/pipeline/build_schemas.py --clean
   ```

6. 生成的企业级 schema 将按照域分类输出到 `dist/json/`，后续可扩展生成 YAML/文档等成果。

## 后续扩展建议

- 将 `run_pipeline` 包装为 CLI 工具，增加差异比对与指标输出。
- 在 CI 中执行，结合单元测试验证 `resolve_refs`、`infer_domain` 等关键函数。
- 与 `docs/` 中的计划文档协同，持续完善企业级元数据和多格式发布能力。
