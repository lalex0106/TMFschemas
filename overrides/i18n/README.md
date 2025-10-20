# 多语言翻译说明

该目录用于存放企业自定义的多语言翻译文件，构建流水线会在生成 JSON Schema 时自动将翻译写入 `x-i18n` 字段。

## 推荐目录结构

```
overrides/
  i18n/
    workbooks/
      translations.xlsx      # 可选，通过脚本导出的汇总模板
    zh-CN/
      Schemas_ZH.csv         # 传统 CSV 维护方式
      Properties_ZH.csv
      yaml/
        Account.yaml         # 由脚本生成的 YAML 翻译文件
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

流水线会自动在所有出现该属性的节点上附加对应的 `x-i18n`，包括嵌套对象或引用属性。如发现全局配置影响了某个特定模型，可通过上文所述的 `Schema` 列进行局部覆盖。

> 如需更复杂的结构（例如覆盖 `items`、`definitions` 等），仍可提供 YAML/JSON 文件，流水线会自动合并 CSV 与 YAML/JSON 的内容。

## 使用 Excel 工作簿批量维护翻译

若希望在单一 Excel 中集中维护所有翻译，可配合 `tools/pipeline/i18n_workbook.py` 使用：

1. 执行 `python tools/pipeline/i18n_workbook.py extract` 导出模板，默认写入 `overrides/i18n/workbooks/translations.xlsx`。
2. 在 `Schemas`、`Properties` 工作表中填写 `中文名称`、`中文描述` 列（英文列仅做参考）。
3. 执行 `python tools/pipeline/i18n_workbook.py render`，脚本会自动在 `overrides/i18n/zh-CN/yaml/` 下生成分模型 YAML，示例结构：

   ```yaml
   schema: Account
   translations:
     title: "账户"
     description: "通用账户结构，用于描述客户账户与金融账户之间的公共特性。"
     properties:
       name:
         title: "账户名称"
         description: "账户在界面上展示的名称。"
       creditLimit:
         description: "账户可以被透支或消费的最高额度。"
   ```

4. 重新运行构建流水线，最新翻译即会写入 `x-i18n`。

如果某些属性需要全局共享翻译，可在 `Properties` 工作表保留空的 `Schema` 列，生成的 YAML 会写入 `__GLOBAL_PROPERTIES__.yaml`，供流水线在所有模型中复用。

流水线会保留英文原文，并在 `x-i18n` 中写入对应语种的内容，方便团队按需渲染。
