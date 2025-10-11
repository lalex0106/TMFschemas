# TM Forum Open API Schema 仓库（企业版）

本仓库在官方早期 JSON Schema 的基础上，逐步引入 TM Forum 发布的 YAML/JSON 规范，构建企业级数据模型资产库。所有文档与说明均以中文呈现，便于团队协同与扩展治理。

## 目录结构

| Level-0 数据域 | 说明 |
| --- | --- |
| [MarketingSales](./MarketingSales) | 支撑市场与销售活动的实体模型。 |
| [Product](./Product) | 聚焦产品生命周期管理与目录治理。 |
| [Customer](./Customer) | 客户、账户、结算等相关实体。 |
| [Service](./Service) | 服务设计、编排与运营支撑模型。 |
| [Resource](./Resource) | 网络、应用及基础资源层实体。 |
| [BusinessPartner](./BusinessPartner) | 合作伙伴及利益相关方管理实体。 |
| [Enterprise](./Enterprise) | 企业级共享能力与内部支撑实体。 |
| [Common](./Common) | 通用能力与跨域复用的公共实体。 |

除此之外：

- `sources/`：存放官方 YAML/JSON 原始文件及外部参考源。
- `overrides/`：企业自定义扩展与覆盖层的占位目录。
- `overrides/i18n/`：存放多语言翻译文件，生成的 Schema 会带有 `x-i18n` 便于内部阅读，同时在 `dist/validation/` 下输出移除扩展后的校验版本。
- `tools/pipeline/`：YAML 转换与引用重写流水线原型代码。
- `dist/`：构建输出目录，后续用于发布 JSON Schema、YAML 及文档。

## 设计约定

- 全部 Schema 遵循 JSON Schema draft-07，并逐步补充企业级 `x-metadata` 描述；流水线会额外输出去除 `x-*` 扩展字段的校验副本。
- 目录命名与文件命名保持与官方一致，方便进行差异比对与自动化同步。
- 详细的计划与落地步骤见 `docs/` 目录中的配套文档。

## 快速开始

1. 按照 `sources/tmf-official/README.md` 指引同步官方 API 规范文件。
2. 根据需要更新 `config/domain_mapping.yaml` 与 `config/name_mapping.json`，统一域归属与命名映射。
3. （可选）在 `overrides/i18n/<locale>/` 下补充翻译文件。推荐使用 Excel 维护并导出 `Schemas_ZH.csv`、`Properties_ZH.csv` 等 CSV。   其中 `Properties_ZH.csv` 可仅保留 `Property,Descriptions,中文名称,新描述` 四列，流水线会自动把中文名称/描述应用到所有同名属性；若发现不同模型下同名属性含义不一，可新增一行写入 `Schema` 列（或直接在 `Property` 中写成 `模型.属性`），为指定模型设置专属翻译，避免被全局配置覆盖。
4. 执行流水线原型：

   ```bash
   python tools/pipeline/build_schemas.py --clean
   ```

5. 在 `dist/json/` 目录查看带有企业扩展和多语言信息的 Schema，并结合文档持续优化；同步会在 `dist/validation/` 中生成去除 `x-i18n`、`x-metadata` 等扩展字段的严格版本。
6. （推荐）执行 `python tools/pipeline/run_validation.py` 触发 TMF 官方校验脚本，结果会保存在 `dist/validation/validation_results.txt`。日志里如仅出现 `x-i18n`、`x-metadata` 等字段被禁止，可判定为我们主动的企业增强；若提示缺少 `type`、`discriminator` 等关键字段，则属于需要修复的结构性问题。

如需贡献改进，请在提交前确保文档与代码同步更新，并遵循本仓库的中文编写约定。
