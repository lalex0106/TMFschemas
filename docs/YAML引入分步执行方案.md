# YAML 引入与企业 Schema 构建分步执行方案

为贯彻“先分析映射、再生成重写”的总体思路，本方案结合新建的流水线原型，明确引入官方
YAML 并扩展为企业 schema 的具体步骤。所有章节均以中文撰写，便于团队内协作。

## 1. 准备阶段

1. **目录落位**：
   - `sources/tmf-official/`：存放官方 Open API YAML。已在仓库创建并通过 `.gitkeep` 占位。
   - `sources/external/`：用于补充外部标准或企业内契约。亦已准备占位。
   - `overrides/enterprise/`：未来承载企业自定义覆盖文件。
2. **配置基线**：
   - `pipeline.config.yaml` 定义数据源、阈值和输出目录，可根据环境进行覆盖。
   - `config/domain_mapping.yaml` 记录 API -> 数据域映射，继承本地脚本的分类经验。
   - `config/name_mapping.json` 用于维护历史名称与新名称的映射，默认提供空对象。

## 2. 分析与映射（对应脚本 Phase 1）

1. 运行 `tools/pipeline/build_schemas.py` 的分析流程，自动执行：
   - 统计 YAML 中 `components.schemas` 的使用频次；
   - 应用阈值（默认 12 次）判定通用模型；
   - 结合 `domain_mapping.yaml`、历史 JSON Schema 目录推断模型所属域。
2. 如需要人工校正，可在 `config/domain_mapping.yaml` 中调整，或在 `config/name_mapping.json`
   增补命名映射以消除历史差异。
3. 输出日志中若出现 `⚠️ 解析失败`，需定位对应 YAML 并修复语法问题。

## 3. 生成与重写（对应脚本 Phase 2）

1. 选择性执行 `--clean` 参数以清空既有输出，避免旧文件干扰。
2. 脚本会对每个模型执行：
   - 深拷贝 schema，调用 `resolve_refs` 递归重写 `$ref`，保持跨域引用的相对路径；
   - 为模型生成包含 `$schema`、`$id`、`x-metadata.domain` 的 JSON Schema 文档；
   - 保存至 `dist/json/<Domain>/<Model>.schema.json`。
3. 后续计划将在此基础上扩展：
   - 生成 YAML/Markdown 文档 (`dist/yaml/`、`dist/docs/`)；
   - 引入企业级元数据（如保密级别、数据主责任人）。

## 4. 渐进式扩展

1. **外部源融合**：逐步向 `sources/external/` 添加行业标准或业务契约，并在配置文件中更新阈值。
2. **企业覆盖**：设计 `overrides/enterprise/` 的结构与合并策略，使企业扩展与官方版本可追溯。
3. **质量保障**：
   - 为 `tools/pipeline/build_schemas.py` 编写单元测试，覆盖 `infer_domain`、`resolve_refs` 等关键逻辑；
   - 在 CI 中集成 schema 校验、示例实例验证与差异报告。
4. **文档联动**：持续更新《代码库审查与完善计划》，同步成果与下一阶段里程碑。

## 5. 即刻行动清单

- [x] 建立源/输出目录及配置文件占位。
- [x] 引入配置化的流水线原型脚本，并复用用户脚本的域判定与引用重写优势。
- [ ] 补充官方 YAML 与企业扩展样例，验证产出效果。
- [ ] 构建自动化测试与发布流程，实现端到端验证。

> 本方案与 `tools/pipeline/README.md` 互为补充，可指导团队成员按阶段逐步落地企业化 schema 构建。
