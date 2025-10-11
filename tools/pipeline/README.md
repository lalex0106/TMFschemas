# 官方 YAML 到企业 Schema 流水线（原型）

本目录提供一个基于 Python 的最小可运行原型，用于演示如何将官方发布的 Open API YAML
转换为企业化管理的 JSON Schema 资产。脚本吸收了用户提供的本地脚本优点，强化了以下特性：

- **配置驱动**：全部路径、阈值与元数据通过 `pipeline.config.yaml` 管理，便于在不同环境中复用。
- **阶段清晰**：保持“分析映射 -> 生成重写”两阶段设计，先确定域归属，再重写 `$ref`。
- **渐进落地**：输出目录与源目录均已在仓库中占位，可逐步补齐官方与企业扩展的原始文件。
- **容错日志**：解析失败时输出告警，不阻塞整体流程，便于后续排查质量问题。

## 使用步骤

1. 将官方 YAML 文件复制或同步至 `sources/tmf-official/`，支持多级目录。
2. 如有额外参考源，可放入 `sources/external/`。
3. 根据需要编辑 `config/domain_mapping.yaml` 与 `config/name_mapping.json`，统一域映射与命名规范。
4. 执行：

   ```bash
   python tools/pipeline/build_schemas.py --clean
   ```

5. 生成的企业级 schema 将按照域分类输出到 `dist/json/`，后续可扩展生成 YAML/文档等成果。

## 后续扩展建议

- 将 `run_pipeline` 包装为 CLI 工具，增加差异比对与指标输出。
- 在 CI 中执行，结合单元测试验证 `resolve_refs`、`infer_domain` 等关键函数。
- 与 `docs/` 中的计划文档协同，持续完善企业级元数据和多格式发布能力。
