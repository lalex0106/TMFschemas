# 多语言翻译说明

该目录用于存放企业自定义的多语言翻译文件，构建流水线会在生成 JSON Schema 时自动将翻译写入 `x-i18n` 字段。

## 推荐目录结构

```
overrides/
  i18n/
    zh-CN/
      Schemas_ZH.csv
      Properties_ZH.csv
```

- `Schemas_ZH.csv`：面向模型本身的翻译，列建议包含 `Schema`、`Descriptions`（可选，仅作原文备注）、`中文名称`、`新描述`。
- `Properties_ZH.csv`：面向属性的翻译，可仅保留 `Property`、`Descriptions`（原文可选）、`中文名称`、`新描述` 四列，流水线会把同名属性翻译应用到全部模型。
  - 若某个属性在不同模型中的含义不同，可额外增加 `Schema` 列，或直接在 `Property` 列使用 `模型.属性`（如 `Account.description`）的写法，流水线会优先采用带模型限定的翻译，避免覆盖其他模型。

翻译人员可直接使用 Excel 编辑并导出为 CSV，流水线会自动识别 UTF-8 或带 BOM 的编码。示例：

```
Schema,Descriptions,中文名称,新描述
Account,通用账户,账户,通用账户结构，用于描述客户账户与金融账户之间的公共特性。
```

对于属性翻译，可直接使用四列表头：

```
Property,Descriptions,中文名称,新描述
name,,名称,在界面或报表中展示的标准名称。
creditLimit,,信用额度,账户可以被透支或消费的最高额度。
Account.description,,账户描述,仅对 Account 模型生效的补充说明。
```

> 如需更复杂的结构（例如覆盖 `items`、`definitions` 等），仍可提供 YAML/JSON 文件，流水线会自动合并 CSV 与 YAML/JSON 的内容。

流水线会保留英文原文，并在 `x-i18n` 中写入对应语种的内容，方便团队按需渲染。
