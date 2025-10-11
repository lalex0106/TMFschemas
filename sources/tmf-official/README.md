# TMF 官方 YAML/JSON 源文件整理指南

为对接 TM Forum 官方发布的 Open API/AsyncAPI 规范，本目录按“版本 → 协议类型 → 原始文件”的层级归档。导入规则如下：

1. **版本分层**：
   - 将官方仓库中 `API-v1` ~ `API-v5` 等目录逐一映射到本仓库同名子目录。
   - 若未来新增版本，例如 `API-v6`，可直接在本目录下创建对应文件夹并沿用相同结构。
2. **协议类型拆分**：
   - `openapi/`：存放 REST/OpenAPI 规范文件，沿用官方的 `*.oas.yaml` 命名，例如 `TMF622-ProductOrdering-v5.0.0.oas.yaml`。
   - `asyncapi/`：存放事件或消息驱动接口，沿用官方的 `*.asyncapi.json`（或 `.yaml`）命名，例如 `TMF620e-Product_Catalog_Management-v5.0.0.asyncapi.json`。
3. **保留原始命名**：除必要的目录重组外，不改动官方文件名，便于与外部资料比对与脚本自动识别。
4. **增量同步建议**：
   - 每次从官方仓库拉取更新后，优先将新文件放入对应版本与协议类型目录，再执行 `tools/pipeline/build_schemas.py`。
   - 若同一 API 跨版本存在差异，可在各版本目录下分别保留，后续由流水线根据 `pipeline.config.yaml` 中的规则进行选择或合并。

> 小贴士：若已在本地完成初步整理，可直接将 `API-v*/` 的子目录复制到此处并覆盖 `.gitkeep` 占位符。流水线会自动扫描 `openapi/` 与 `asyncapi/` 目录中的所有规范文件。
