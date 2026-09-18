# DevSupport Agent V2 — 实施计划

> 状态：设计基线
>
> 关联：[V2_SCOPE.md](V2_SCOPE.md)、[V2_PRODUCT_DESIGN.md](V2_PRODUCT_DESIGN.md)、[V2_TECH_DESIGN.md](V2_TECH_DESIGN.md)。

## 1. 实施规则

V2 允许较大重写，但每次只完成一个可验证子任务。不得为了兼容 V1 的审批、回滚或恢复验证而偏离 V2 的只读调查定位。

每个实现任务必须先写明：

* 目标；
* 要检查和复用的现有实现；
* 所需变更；
* 明确非目标；
* 验证方式与完成定义。

每个完成的子任务必须运行相关测试、`git diff --check`、`git status --short`，创建独立 Git commit，并推送到 `origin/main` 后停止。

## 2. 迁移策略

1. V1 文档和实现保留为历史参考，V2 文档不修改它们的事实记录；
2. 先建立 V2 领域模型和只读运行路径，再移除 V2 路径中的旧修复语义；
3. 仅迁移被 V2 明确保留的数据与能力；不承诺保持 V1 API、数据库 Schema 或 UI 的兼容；
4. 数据库变化必须通过 Alembic Migration；
5. 在新的 V2 Eval 证明替代能力前，不删除可用于参考的 Fault Lab 与既有测试资产。

## 3. 里程碑

### M0 — V2 设计基线

| 子任务 | 目标 |
| --- | --- |
| M0.1 | 固化 V2 范围、产品、技术与实施设计文档。 |
| M0.2 | 盘点可复用的 V1 模块和必须移除的修复路径，形成迁移清单。 |

完成标准：V2 的只读边界、终态、知识隔离、调查轮次和部署假设没有冲突。

### M1 — V2 领域模型与只读边界

| 子任务 | 目标 |
| --- | --- |
| M1.1 | 定义并迁移 `InvestigationTarget`、服务范围、Incident、InvestigationRound、Observation 与版本化 Report。 |
| M1.2 | 将 V2 Incident 状态收束为 `OPEN`、`INVESTIGATING`、`CONCLUDED`、`INCONCLUSIVE`、`FAILED`。 |
| M1.3 | 建立只读 V2 Tool Registry，并确保 V2 API、Workflow 和 UI 不可达旧修复路径。 |
| M1.4 | 建立历史轮次不可变性和新轮次关联的测试。 |

完成标准：可创建 V2 Incident、启动一轮调查、持久化终态；V2 Runtime 不含副作用 Tool。

### M2 — 调查目标与 Adapter Runtime

| 子任务 | 目标 |
| --- | --- |
| M2.1 | 定义部署期调查目标配置模型、服务白名单、能力矩阵和 Secret 引用边界。 |
| M2.2 | 将 Logs、Metrics、Traces、Deployment Facts Adapter 的选择绑定到当前目标，而非全局固定 Provider。 |
| M2.3 | 为 Adapter 输入、超时、错误映射、结果标准化和范围拒绝添加契约测试。 |
| M2.4 | 保留 Fault Lab 作为确定性测试目标；完成 OpenTelemetry Demo 的 Logs + Metrics V2 接入验收。 |

完成标准：一个已配置目标中的新服务可用已有 Adapter 调查；未配置服务、环境或 Provider 不能被查询。

### M3 — 隔离知识库与 RAG

| 子任务 | 目标 |
| --- | --- |
| M3.1 | 将知识文档扩展为 target、scope、service、environment、version、status 等明确元数据。 |
| M3.2 | 实现 Markdown 上传、解析、原子摄取、版本与启用状态。 |
| M3.3 | 在关键词与向量检索两侧实施目标/服务/环境/状态过滤。 |
| M3.4 | 实现 Citation 投影与知识范围泄漏回归测试。 |

完成标准：不同调查目标、服务和停用文档不能互相进入检索结果；所有关键知识结果可回溯到文档和章节。

### M4 — 可靠调查工作流

| 子任务 | 目标 |
| --- | --- |
| M4.1 | 以 V2 领域模型重建或迁移持久化的假设—Evidence 调查循环。 |
| M4.2 | 实现统一预算、结构化失败分类、明确终态和可靠的进度投影。 |
| M4.3 | 约束 LLM 输出、Evidence / Citation 引用和结论强度。 |
| M4.4 | 实现“补充信息并继续调查”，保留旧轮次快照并产生新的 Report。 |

完成标准：调查可恢复、可解释、有预算；异常或证据不足时安全终止，不伪造结论。

### M5 — 中文 Web Console

| 子任务 | 目标 |
| --- | --- |
| M5.1 | 重做中文首页、Incident 创建与调查目标选择。 |
| M5.2 | 重做调查详情：结论、证据、用户时间线、建议、轮次报告和技术详情。 |
| M5.3 | 实现补充观察并继续调查的交互。 |
| M5.4 | 实现 Markdown 知识库管理与上传状态页面。 |

完成标准：非熟悉 Workflow 的用户能完成创建、理解结论、查阅证据、补充观察和查看历史轮次；技术细节不干扰主流程。

### M6 — 内部试用部署与 V2 Release Gate

| 子任务 | 目标 |
| --- | --- |
| M6.1 | 补齐 Docker Compose 部署、环境变量说明、健康检查和数据库备份运行手册。 |
| M6.2 | 建立 V2 Eval：正常调查、错误注入、预算、Adapter、知识隔离和轮次延续。 |
| M6.3 | 验证 Fault Lab 确定性回归与 OpenTelemetry Demo Logs + Metrics 集成。 |
| M6.4 | 在可信内网/本机部署模型中完成浏览器端手工验收。 |

完成标准：V2 能被部署到可信内网环境，核心 Eval 稳定通过，且所有 V2 对外路径均为只读。

## 4. V2 Release Gate

V2 可进入内部试用，至少需要：

* 创建、调查、结论、报告和继续调查主线可用；
* `CONCLUDED`、`INCONCLUSIVE`、`FAILED` 均有清晰、持久化的原因；
* 结论中的关键证据和知识可追溯；
* 知识检索的 target / service / environment 隔离测试通过；
* Logs + Metrics 的受控 Adapter 查询与错误边界测试通过；
* Fault Lab 回归和 OpenTelemetry Demo 真实集成验收通过；
* V2 Tool Registry、API 和 UI 不含写操作、审批、回滚或恢复验证入口；
* 中文 UI 能让用户完成主要流程；
* Docker Compose 内网部署、Secret 配置、健康检查和数据库备份说明可用。

## 5. 明确顺序

M1 是其余开发的前置。M2 与 M3 可在 M1 基础上并行规划，但同一工作树中仍应逐个子任务实施。M4 依赖 M1 至 M3 的稳定边界。M5 不得自行推导业务规则。M6 只验证实现真相，不通过降低 Ground Truth、删除测试或隐藏错误让发布通过。
