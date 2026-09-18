# DevSupport Agent V2 — 技术设计

> 状态：设计基线
>
> 关联：[V2_SCOPE.md](V2_SCOPE.md)、[V2_PRODUCT_DESIGN.md](V2_PRODUCT_DESIGN.md)。本文描述 V2 的目标边界，不要求兼容 V1 的修复工作流。

## 1. 设计原则

1. **只读优先**：V2 Agent 运行路径没有任何副作用 Tool；
2. **证据优先**：LLM 可以提出假设和解释，但不能将无来源内容当作事实；
3. **范围先于相关性**：调查目标、服务和环境过滤必须先于向量或关键词排名；
4. **持久化先于展示**：浏览器显示的是后端已持久化的状态投影；
5. **有限失败优于无限尝试**：超时、重试、调用次数和调查轮次均受预算约束；
6. **配置与代码分离**：Adapter 是受审查的代码；目标地址、Secret 与服务映射是部署配置；
7. **失败可解释**：外部数据源、模型、解析或状态错误都必须有结构化分类和用户可读说明。

## 2. 总体架构

```text
中文 Web Console
        ↓
FastAPI API / 调查投影
        ↓
持久化 Agent Orchestrator
        ├─ 调查轮次状态与 PostgreSQL Checkpoint
        ├─ LLM：假设、计划、证据解释、结论
        ├─ Tool Runtime：Schema、白名单、预算、审计
        ├─ RAG：范围过滤、混合检索、Citation
        └─ Report Builder
        ↓
Adapter Contract
        ├─ Logs Adapter
        ├─ Metrics Adapter
        ├─ Traces Adapter（可选）
        └─ Deployment Facts Adapter（可选且只读）
        ↓
已配置的日志 / 指标 / Trace / 部署数据平台
```

V2 可复用现有 FastAPI、PostgreSQL、LangGraph、Pydantic、SQLAlchemy、pgvector 与 PostgreSQL FTS 基础，但允许重构领域模型、API 和 UI。

## 3. 调查目标与配置边界

每个 `InvestigationTarget` 至少包含：

```text
id、显示名称、描述
服务白名单与服务标识映射
可用能力：logs / metrics / traces / deployments
每种能力所绑定的 Adapter 类型
对应数据源的地址与 Secret 引用
关联的知识范围
```

V2 P0 的目标配置可以来自部署配置文件或环境变量，不要求产品内自助配置 UI。Secret 仅在后端运行环境可见，绝不写入数据库普通字段、浏览器响应、Prompt 或 Tool History。

### 3.1 Adapter 模式

Agent 只调用稳定 Tool：

```text
query_logs(service, time_range, query, limit)
query_metrics(service, time_range)
query_traces(service, time_range, correlation)
search_knowledge(query, scope)
```

Tool Runtime 根据当前调查目标选择已注册的 Adapter。Adapter 将统一输入翻译为 Provider API 请求，并将结果转换为有限的标准结构。

```text
query_logs
→ Tool Runtime 校验目标、服务、时间范围和预算
→ OpenSearchLogsAdapter / LokiLogsAdapter / 其他已注册实现
→ Provider API
→ 标准化 LogQueryResult
→ 持久化 Runtime Evidence
```

接入另一个同样使用 OpenSearch 与 Prometheus 的系统，只需新增目标配置；接入 Loki、Datadog 等新的 Provider 时才开发并测试新的 Adapter。V2 不支持任意 HTTP 请求、任意 Shell、任意 SQL 或 Agent 生成代码执行。

### 3.2 输入与输出约束

每个 Tool 必须有 Pydantic Input / Result Schema，并具备：

* 目标、服务、环境、时间范围与结果上限校验；
* 明确 HTTP 或 Provider 调用超时；
* 可重试与不可重试错误分类；
* Provider 响应解析和边界化输出；
* 来源、查询时间、查询条件与关联 ID 等 Evidence provenance；
* 绝不向 LLM 暴露 Secret、完整原始 Provider 文档或任意内部网络地址。

## 4. RAG 与知识隔离

### 4.1 知识空间

知识文档必须带有：

```text
target_id（必填）
scope：shared / service
service_id（service scope 必填）
environment：common / 指定环境
document_type、title、source、version、status、content_hash
```

默认检索范围为：当前目标 + 当前服务 + 目标共享知识 + 当前环境/通用知识。

若运行 Evidence 显示问题涉及目标内另一服务，Workflow 可以通过受控的依赖服务步骤扩展到该服务范围。它不得跨调查目标扩展，也不得直接检索所有服务的私有知识。

### 4.2 检索流程

```text
检索请求
→ 强制 target / service / environment / document-status Filter
→ 关键词检索（FTS）与向量检索（pgvector）
→ RRF 融合与稳定排序
→ 返回内容片段、文档、章节和 Citation
```

所有过滤必须同时应用于关键词和向量检索。无 Citation 的知识不得成为关键结论依据。上传内容被视为数据而非 Agent 指令；只有可信团队资料才能被启用。

### 4.3 摄取流程

V2 P0 仅接收 Markdown：

```text
上传
→ 元数据校验
→ Markdown 解析
→ 切分
→ 生成 Embedding
→ 原子化写入文档与 Chunk
→ 启用索引
```

失败、格式不合法或 Embedding 不完整时不得部分启用文档。更新同一逻辑文档应形成可追溯版本，旧版本默认不参与检索。

## 5. 调查状态、记忆与报告

V2 不使用无限增长的聊天历史作为记忆。它分别持久化：

| 类型 | 内容 |
| --- | --- |
| 工作记忆 | 当前轮次的目标、假设、Evidence、Tool History、预算、错误与当前待验证问题。 |
| 团队知识 | 已审核并按范围隔离的知识文档与 Citation。 |
| 协作记录 | Incident 输入与用户补充观察，均标记来源与时间。 |

每个 Incident 包含一个或多个 `InvestigationRound`。新轮次引用历史数据但不改写历史报告、Evidence 或结论。报告是该轮次的不可变快照，至少包含：输入摘要、结论、置信度、支持/反驳证据、未确认事项、人工建议、时间线和终态原因。

## 6. 可靠性边界

### 6.1 LLM 的有限职责

LLM 仅负责生成受 Schema 约束的候选假设、调查计划、Evidence 解释与结论草稿。确定性代码负责：

* Tool 名称和输入合法性；
* Evidence ID 与 Citation 存在性；
* 预算、重试、状态转移和终态；
* 目标、服务、环境与知识范围；
* 持久化和报告绑定。

无 Evidence ID 的结论不能标记为已支持；证据矛盾、数据缺失或外部调用失败时，应降低结论强度或终止为 `INCONCLUSIVE` / `FAILED`。

### 6.2 预算与失败

每轮调查至少有：总时限、单次 LLM 超时、LLM 调用上限、Tool 调用上限、最大轮次和有限重试预算。失败分类至少区分：输入错误、配置错误、Provider 不可用、Provider 响应无效、LLM 超时、LLM 输出无效、预算耗尽和持久化错误。

任何失败不得产生虚构 Evidence 或“已恢复”结论。

### 6.3 Eval

V2 Eval 必须覆盖：

* 正常故障方向、关键 Evidence 和 Tool 选择；
* 日志/指标 Adapter 超时或错误；
* LLM 超时和非 Schema 输出；
* 预算耗尽与终态原因；
* 知识范围隔离、停用文档和跨目标泄漏；
* 补充观察后的新轮次、历史不可变性和结论修订；
* V2 Runtime 没有副作用 Tool；
* OpenTelemetry Demo 的日志与指标真实接入验收。

Fault Lab 是确定性回归与 Ground Truth 环境；OpenTelemetry Demo 是 Provider 泛化验收环境。二者都不等同于最终用户的生产环境。

## 7. 部署边界

V2 P0 支持 Docker Compose 部署到可信内网或 VPN 环境。部署应包含 Web、Backend 和 PostgreSQL，使用非默认生产凭据、环境变量/Secret、健康检查与数据库备份说明。

本地开发不强制 HTTPS 或反向代理。共享服务器部署时，应由部署方提供私有网络边界；需要浏览器跨网络访问时再增加 HTTPS 与反向代理。高可用、Kubernetes、自动扩缩容与公网安全体系不是 V2 P0。
