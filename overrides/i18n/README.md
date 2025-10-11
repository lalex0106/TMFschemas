# 多语言翻译说明

该目录用于存放企业自定义的多语言翻译文件，构建流水线会在生成 JSON Schema 时自动将翻译写入 `x-i18n` 字段。

目录结构建议如下：

```
overrides/
  i18n/
    zh-CN/
      Account.yaml
```

每个翻译文件均使用 YAML 或 JSON 编写，推荐结构如下：

```yaml
schema: Account
translations:
  title: "账户"
  description: "通用账户结构……"
  properties:
    name:
      title: "账户名称"
      description: "用于展示的账户名称。"
```

- `schema`（可选）：显式指定对应的模型名称；若缺失则默认使用文件名。
- `translations`：待写入的翻译内容。若无此字段，则会把除 `schema` 之外的字段视为翻译内容。
- 目前支持为 `title`、`description`、`summary` 以及 `properties`、`items`、`allOf`/`anyOf`/`oneOf` 等结构补充翻译。

流水线会保留英文原文，并在 `x-i18n` 中写入对应语种的内容，方便团队按需渲染。
