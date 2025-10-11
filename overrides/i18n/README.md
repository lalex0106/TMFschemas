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
- `Properties_ZH.csv`：面向属性的翻译，列建议包含 `Schema`、`Property`、`中文名称`、`新描述`。若没有单独的 `Property` 列，可以在 `Schema` 中使用 `模型.属性` 的写法。

翻译人员可直接使用 Excel 编辑并导出为 CSV，流水线会自动识别 UTF-8 或带 BOM 的编码。示例：

```
Schema,Descriptions,中文名称,新描述
Account,通用账户,账户,通用账户结构，用于描述客户账户与金融账户之间的公共特性。
```

对于属性翻译：

```
Schema,Property,中文名称,新描述
Account,name,账户名称,账户在界面上展示的名称。
Account,creditLimit,,账户可以被透支或消费的最高额度。
```

> 如需更复杂的结构（例如覆盖 `items`、`definitions` 等），仍可提供 YAML/JSON 文件，流水线会自动合并 CSV 与 YAML/JSON 的内容。

流水线会保留英文原文，并在 `x-i18n` 中写入对应语种的内容，方便团队按需渲染。
