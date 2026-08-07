# DevSupport Agent V0 — Technical Design

> Technical Design Document
> Status: Draft for Technical Freeze
> Related: `docs/PRD.md`
> Version: V0

---

# 1. 文档目标

本文档回答：

> DevSupport Agent V0 在技术上由哪些系统组成，各模块分别负责什么，它们之间如何通信和共享状态。

本文档重点确定：

* 整体系统架构；
* 模块职责；
* 模块边界；
* 核心数据流；
* Agent 与业务系统的关系；
* RAG、Tools、Workflow、实验环境之间的关系；
* PostgreSQL 在系统中的职责；
* 运行与部署形态。

本文档暂不展开：

* 每张数据库表的完整字段；
* 每个 API 的完整 Request / Response；
* LangGraph 每个 State 字段；
* 每个 Tool 的具体 Pydantic Model；
* 完整前端组件设计；
* 详细代码目录。

这些内容在实施阶段根据模块继续细化。

---

# 2. 技术设计原则

V0 遵循以下原则。

## 2.1 Agent 是编排者，不是数据源

LLM 负责：

* 理解 Incident；
* 生成候选假设；
* 判断下一步需要什么证据；
* 选择允许的 Tool；
* 根据证据更新判断；
* 形成处置建议和报告。

LLM 不负责：

* 自己生成日志；
* 自己生成指标；
* 自己猜测发布记录；
* 自己宣布回滚成功；
* 自己决定审批结果。

所有事实必须来自 RAG 或 Runtime Tools。

---

## 2.2 业务状态和 Agent 状态必须持久化

不能设计成：

```text
HTTP Request
→ Agent 跑几十秒
→ 返回结果
→ 所有中间状态消失
```

因为调查过程中存在：

```text
开始调查
→ 多轮工具调用
→ WAITING_APPROVAL
→ 用户稍后批准
→ 恢复执行
```

所以 Agent Workflow 必须是一个持久化任务。

---

## 2.3 Tool 是安全边界

Agent 不直接：

```text
访问数据库
执行 Shell
访问 Docker Socket
执行任意 HTTP 请求
```

Agent 只能调用系统显式注册的 Tool。

例如：

```text
query_logs()
query_metrics()
query_traces()
get_deployment_history()
rollback_deployment()
```

每个 Tool 内部负责：

* 参数校验；
* 权限检查；
* 查询 / 执行；
* 返回结构化数据；
* 超时；
* 异常转换；
* Trace。

---

## 2.4 PostgreSQL 作为 V0 核心数据底座

V0 不引入多个独立业务数据库。

统一使用：

> PostgreSQL + pgvector

负责：

1. Incident 业务数据；
2. Agent 调查状态相关业务数据；
3. Approval；
4. Evidence；
5. Tool Call Record；
6. Report；
7. Knowledge Document Metadata；
8. Knowledge Chunks；
9. Embedding Vector；
10. Full-Text Search；
11. Eval 结果；
12. LangGraph 持久化所需数据库能力。

目的不是让所有逻辑塞进一个数据库，而是减少 V0 基础设施复杂度。

---

# 3. 总体系统架构

整体分为六层：

```text
┌───────────────────────────────────────────────┐
│                 Web Console                   │
│                                               │
│ Incident 创建 / 调查状态 / Approval / Report │
└──────────────────────┬────────────────────────┘
                       │ HTTP / SSE
                       ↓
┌───────────────────────────────────────────────┐
│              DevSupport Backend               │
│                  FastAPI                      │
│                                               │
│ Incident API / Approval API / Query API       │
│ Workflow Service / Report Service             │
└──────────────────────┬────────────────────────┘
                       │
                       ↓
┌───────────────────────────────────────────────┐
│                Agent Runtime                  │
│                  LangGraph                    │
│                                               │
│ Intake                                        │
│ Retrieval                                     │
│ Hypothesis                                    │
│ Investigation                                 │
│ Resolution                                    │
│ Approval                                      │
│ Execution                                     │
│ Verification                                  │
│ Report                                        │
└───────────────┬─────────────────┬─────────────┘
                │                 │
         ┌──────▼──────┐   ┌──────▼──────────┐
         │ RAG Service │   │   Tool Layer    │
         │             │   │                 │
         │ Hybrid      │   │ logs            │
         │ Retrieval   │   │ metrics         │
         │ Metadata    │   │ traces          │
         │ Citation    │   │ deployments     │
         └──────┬──────┘   │ rollback        │
                │          └────────┬─────────┘
                │                   │
                ↓                   ↓
┌────────────────────────┐   ┌────────────────────────┐
│ PostgreSQL + pgvector  │   │ Fault Lab              │
│                        │   │                        │
│ incidents              │   │ order-service          │
│ evidence               │   │      ↓                 │
│ approvals              │   │ payment-service        │
│ documents / chunks     │   │                        │
│ vectors / full text    │   │ logs / metrics / trace │
│ eval results           │   │ deployment state       │
│ workflow persistence   │   │ fault injection        │
└────────────────────────┘   └────────────────────────┘
```

---

# 4. 系统模块划分

V0 划分为以下九个主要模块：

```text
1. Web Console
2. Incident Service
3. Agent Workflow
4. RAG Service
5. Tool Layer
6. Approval & Safety
7. Recovery Verification
8. Fault Lab
9. Eval & Observability
```

其中真正的 Agent 核心是：

```text
Agent Workflow
+ RAG Service
+ Tool Layer
+ Approval
+ Verification
```

其他模块负责提供真实业务环境与工程支撑。

---

# 5. Web Console

## 5.1 职责

Web Console 只负责：

* 创建 Incident；
* 查看 Incident；
* 展示当前 Agent 状态；
* 展示 Hypothesis；
* 展示 Evidence；
* 展示 Tool Timeline；
* Approve / Reject；
* 展示 Recovery Verification；
* 展示 Final Report。

Web 不承担 Agent 业务逻辑。

---

## 5.2 推荐技术

```text
Next.js
TypeScript
```

V0 不要求复杂状态管理框架。

前端优先通过：

```text
REST
+
SSE / 简单轮询
```

获得后端调查状态。

如果 SSE 在 V0 开发中产生明显额外复杂度，可以退化为轮询。

---

# 6. FastAPI Backend

FastAPI 是整个产品的业务入口。

它位于：

```text
Web
↓
FastAPI
↓
Agent / Database / Tools
```

而不是让前端直接调用 LangGraph。

---

## 6.1 Incident Service

负责 Incident 生命周期。

主要职责：

```text
create_incident
get_incident
list_incidents
get_investigation
get_report
```

Incident Service 不判断故障根因。

它只管理业务实体。

---

## 6.2 Workflow Service

负责：

```text
启动 Agent Workflow
查询 Workflow 状态
恢复暂停 Workflow
处理 Workflow 失败
```

例如：

```text
POST /incidents
        ↓
创建 Incident
        ↓
启动 LangGraph Thread
```

Incident ID 与 Agent Thread 建议建立一一关联：

```text
incident_id
↕
thread_id
```

以便后续恢复。

---

## 6.3 Approval Service

负责：

```text
Approve
Reject
```

Approval API 必须独立于模型。

流程：

```text
Agent
→ WAITING_APPROVAL
→ LangGraph interrupt
→ Backend 返回等待状态

用户点击 Approve
→ Approval API
→ 写入 Approval Record
→ Workflow Resume
```

审批状态不允许通过 LLM Tool 参数伪造。

---

# 7. Agent Workflow

Agent Workflow 使用：

> LangGraph

V0 使用单 Agent + 显式状态图。

不采用多 Agent。

---

# 8. LangGraph 节点设计

建议划分为：

```text
intake
    ↓
retrieve_knowledge
    ↓
generate_hypotheses
    ↓
plan_investigation
    ↓
execute_tool
    ↓
update_hypotheses
    ↓
evaluate_evidence
    ├── insufficient → plan_investigation
    └── sufficient
             ↓
      propose_resolution
             ↓
      risk_gate
        ├── no action → report
        └── side effect
                 ↓
              approval
                 ↓
              execute
                 ↓
              verify
             ┌───┴────┐
             ↓        ↓
          success    failure
             ↓        ↓
           report   investigation
```

---

# 9. 节点职责

## 9.1 Intake

输入：

```text
用户提交的 Incident
```

负责：

* 标准化 service；
* environment；
* time range；
* symptoms；
* 判断关键信息是否完整。

不生成根因。

---

## 9.2 Retrieval

负责：

> 在调查开始前获取相关背景知识。

调用：

```text
search_knowledge
```

主要检索：

* service Runbook；
* 历史事故；
* 服务说明；
* 架构说明。

Retrieval 结果进入 Evidence Context。

---

## 9.3 Hypothesis Generation

根据：

```text
Incident
+
Knowledge Evidence
```

生成少量候选假设。

建议：

```text
2～4 个
```

避免一次创建十几个猜测。

---

## 9.4 Investigation Planner

根据当前：

```text
Hypotheses
Evidence
Tool History
```

回答一个问题：

> 下一步最值得验证什么？

然后输出：

```text
next_goal
selected_tool
tool_arguments
```

---

## 9.5 Tool Executor

只负责执行已经通过校验的 Tool。

不负责：

* 判断根因；
* 修改假设；
* 决定是否结束。

---

## 9.6 Hypothesis Update

输入：

```text
Tool Result
+
Current Hypotheses
```

更新：

```text
supporting_evidence
contradicting_evidence
confidence
status
next_check
```

Hypothesis 状态建议：

```text
ACTIVE
SUPPORTED
REJECTED
CONFIRMED
```

---

## 9.7 Evidence Evaluator

判断：

> 当前证据是否已经足以结束调查？

可能结果：

```text
CONTINUE
CONCLUDE
NEEDS_MANUAL_ACTION
```

防止 Agent 无限制循环。

V0 还需要设置硬限制，例如：

```text
最大调查轮数
最大 Tool Calls
```

具体数字在实施阶段配置。

---

## 9.8 Resolution Proposal

根据确认根因和 Evidence 生成：

```text
root_cause
recommended_action
reason
risk
supporting_evidence_ids
```

不能只输出自由文本。

---

## 9.9 Risk Gate

确定推荐操作：

```text
是否有副作用
是否允许环境
是否需要审批
```

例如：

```text
rollback + staging
→ WAITING_APPROVAL

rollback + production
→ DENIED
```

---

## 9.10 Approval

使用 LangGraph interrupt 暂停工作流。

暂停时保存：

```text
Incident
Hypotheses
Evidence
Proposed Action
Current State
```

人工审批后继续。

---

## 9.11 Execution

V0 只允许：

```text
rollback_deployment
```

Execution 不得由模型直接拼接 Shell。

必须走受控 Tool Adapter。

---

## 9.12 Verification

处置完成后进行独立验证。

验证不依赖：

> rollback Tool 返回 success

而是重新查询运行环境。

包括：

```text
health
core API
error signal
critical logs
```

---

## 9.13 Report

生成最终结构化报告。

数据来自：

```text
Incident
+
Hypotheses
+
Evidence
+
Tool Calls
+
Approval
+
Action
+
Verification
```

而不是重新让模型凭记忆总结整个过程。

---

# 10. Agent State 与业务数据库的边界

这里需要明确两个不同概念。

## 10.1 Workflow State

LangGraph State 是：

> Agent 当前执行过程中需要携带的信息。

例如：

```text
incident
hypotheses
evidence_refs
current_goal
tool_history
proposed_action
approval_result
verification
```

---

## 10.2 Domain Data

PostgreSQL 业务表存储：

> 产品长期需要查询和审计的信息。

例如：

```text
incidents
hypotheses
evidence
tool_calls
approvals
actions
verification_results
reports
```

---

## 10.3 原则

不要把整个业务系统只存在 LangGraph State 中。

正确关系：

```text
LangGraph State
= Workflow Runtime State

PostgreSQL Domain Tables
= Product Source of Truth
```

两者允许存在一定信息重复。

---

# 11. LangGraph Persistence

LangGraph 使用持久化 Checkpointer。

V0 优先使用 PostgreSQL 作为持久化后端。

每个 Incident 对应固定：

```text
thread_id
```

使工作流可以：

```text
运行
→ interrupt
→ 进程结束
→ 后续重新启动
→ 根据 thread_id Resume
```

这也是 Human-in-the-loop 能真正成立的基础。

---

# 12. RAG Service

RAG 独立成业务模块，而不是把检索代码散落在 LangGraph Node 中。

接口概念：

```text
RAGService.search(...)
```

Agent 只知道：

```text
query
filters
top_k
```

不需要知道底层 SQL。

---

# 13. RAG 数据架构

使用 PostgreSQL + pgvector。

核心关系：

```text
knowledge_documents
        ↓ 1:N
knowledge_chunks
```

Chunk 同时保存：

```text
content
metadata
embedding
text_search_vector
```

因此一份 Chunk 可以同时参与：

```text
Vector Search
+
Keyword Search
```

---

# 14. Hybrid Retrieval

RAG 检索路径：

```text
Query
  ↓
Metadata Filter
  ↓
┌──────────────┬──────────────┐
│ Vector Search│ Keyword Search│
└──────┬───────┴──────┬───────┘
       ↓              ↓
         Merge / Fusion
               ↓
          Top Candidates
               ↓
       Optional Reranker
               ↓
             Top K
```

V0 优先实现：

```text
vector
+
PostgreSQL FTS
+
简单融合
```

Reranker 为增强能力，不允许阻塞核心流程。

---

# 15. RAG Metadata

至少：

```text
service
environment
document_type
```

例如：

```text
service = order-service
environment IN (staging, common)
document_type IN (runbook, postmortem)
```

尽量在 Retrieval 层过滤，而不是把所有文档召回后让 LLM 自己判断。

---

# 16. Citation

每条 RAG Evidence 都必须有稳定 ID。

例如概念：

```text
document_id
chunk_id
source
section
```

最终报告引用的是 Evidence ID，而不是复制一段无法追踪来源的模型文本。

---

# 17. Tool Layer

Tool Layer 是 Agent 与真实系统之间的适配层。

结构：

```text
Agent
 ↓
Tool Registry
 ↓
Tool
 ↓
Adapter / Client
 ↓
Real Data Source
```

例如：

```text
query_traces
↓
Trace Adapter
↓
Trace Backend
```

---

# 18. 为什么需要 Adapter 层

不能让 LangGraph Node 中直接写：

```text
Prometheus SQL / API
Trace API
Docker 调用
数据库查询
```

否则 Agent Workflow 会与底层基础设施强耦合。

推荐：

```text
tools/
adapters/
```

分离。

以后从轻量日志实现换成 Loki，不需要修改 Agent Graph。

---

# 19. V0 Tool Registry

固定注册：

```text
search_knowledge
query_logs
query_metrics
query_traces
get_deployment_history
rollback_deployment
```

Agent 只能从 Registry 中选择。

不存在：

```text
dynamic tool creation
```

---

# 20. Tool 返回设计原则

Tool 不直接返回大量未经处理的原始数据。

例如 query_logs 不应该返回：

```text
5000 行日志
```

而返回：

```text
时间范围
匹配数量
主要错误模式
first_seen
last_seen
sample records
trace_ids
```

这样：

* Token 更少；
* Agent 更稳定；
* Eval 更容易；
* 数据结构可测试。

---

# 21. Fault Lab

Fault Lab 与 DevSupport Agent 主系统分离。

它本质上是：

> Agent 的可控外部世界。

V0：

```text
order-service
      ↓
payment-service
```

两个真实 FastAPI 服务。

---

# 22. Fault Lab 必须提供

## 正常业务

例如：

```text
POST /orders
```

在正常状态下调用 payment-service 并返回成功。

## Fault Injection

至少：

```text
missing_config
payment_timeout
```

## Reset

所有故障必须支持自动 reset。

## Runtime Evidence

必须产生：

```text
logs
metrics
traces
deployment state
```

Agent 不允许通过：

```text
GET /current-fault
```

这种作弊接口直接获取根因。

---

# 23. Deployment State

V0 不需要真正搭建 Kubernetes / CI/CD。

但需要维护真实可查询的：

```text
service
current_version
previous_version
deployed_at
status
```

rollback_deployment 调用后必须真正改变运行版本或运行配置，使故障消失。

不能只修改数据库中的：

```text
version = old_version
```

然后宣称回滚成功。

---

# 24. Logs / Metrics / Trace

V0 的目标：

> 证据真实，而不是基础设施完整。

因此不要求搭建生产级 Observability Stack。

---

## 24.1 Logs

服务输出结构化日志。

至少包含：

```text
timestamp
service
level
message
trace_id
error_type
```

日志必须可按：

```text
service
time_range
level
```

查询。

---

## 24.2 Metrics

至少产生：

```text
request count
error count / error rate
request latency
```

重点是支持两个故障场景。

---

## 24.3 Trace

至少可以看到：

```text
order-service
   ↓
payment-service
```

的调用关系和耗时。

Scenario B 中 Agent 必须可以从 Trace 判断耗时主要发生于 payment-service。

---

# 25. Approval & Safety Architecture

安全判断不能全部交给 Prompt。

采用两层：

```text
LLM Recommendation
        ↓
Policy Validation
        ↓
Human Approval
```

---

## 25.1 Policy Validation

代码层固定校验：

```text
environment != production
action in allowed_actions
service in allowed_services
```

不满足直接拒绝。

---

## 25.2 Human Approval

通过 Policy 后：

```text
LangGraph interrupt
```

等待用户。

因此即使 LLM 输出：

```text
approved=true
```

也没有实际权限意义。

---

# 26. Recovery Verification

Recovery Verification 独立于 Execution。

结构：

```text
rollback
   ↓
execution_result = success
   ↓
Verification Service
   ├─ health
   ├─ core request
   ├─ metrics
   └─ logs
```

Verification Service 生成：

```text
verification_result
```

最终：

```text
PASS
FAIL
INCONCLUSIVE
```

只有 PASS 可以进入：

```text
RESOLVED
```

---

# 27. Eval Architecture

Eval 不直接依赖 Web。

建立独立：

```text
Eval Runner
```

调用 Backend / Agent Runtime。

基本流程：

```text
Reset Fault Lab
      ↓
Inject Fault
      ↓
Create Incident
      ↓
Run Investigation
      ↓
Handle Approval according to fixture
      ↓
Wait Final State
      ↓
Collect Trace
      ↓
Score
```

---

# 28. Eval Fixture

每个 Fixture 定义：

```text
scenario
incident_input
fault_configuration
expected_root_cause
required_evidence
acceptable_tools
forbidden_actions
approval_behavior
expected_final_status
```

这样 Eval 不靠人工判断。

---

# 29. Agent Observability

V0 不单独建设复杂 Agent Observability 平台。

所有关键执行事件统一产生 Event。

例如：

```text
WORKFLOW_STARTED
NODE_STARTED
LLM_CALLED
RAG_SEARCHED
TOOL_CALLED
TOOL_SUCCEEDED
TOOL_FAILED
HYPOTHESIS_UPDATED
APPROVAL_REQUESTED
APPROVAL_RESOLVED
ACTION_EXECUTED
VERIFICATION_COMPLETED
WORKFLOW_COMPLETED
```

这些事件：

* 写入结构化日志；
* 重要业务记录进入 PostgreSQL；
* 可以被前端 Timeline 使用；
* 可以被 Eval Runner 使用。

---

# 30. 推荐技术栈

## Backend

```text
Python
FastAPI
Pydantic
SQLAlchemy
Alembic
```

## Agent

```text
LangGraph
LLM API
```

## Database

```text
PostgreSQL
pgvector
PostgreSQL Full-Text Search
```

## RAG

```text
Embedding API / Embedding Model
pgvector
PostgreSQL FTS
```

Reranker：

```text
Optional for V0
```

## Fault Lab

```text
Python
FastAPI
OpenTelemetry
```

## Frontend

```text
Next.js
TypeScript
```

## Testing

```text
Pytest
```

## Deployment

```text
Docker
Docker Compose
```

---

# 31. Docker Compose 逻辑拓扑

最终大致运行：

```text
docker compose up
```

启动：

```text
devsupport-backend
postgres
order-service
payment-service
必要 observability components
```

前端可以：

```text
一起 Docker
```

或者开发阶段单独运行。

具体取决于实施成本。

---

# 32. 推荐代码边界

最终仓库大方向建议：

```text
devsupport-agent/
│
├── apps/
│   ├── backend/
│   └── web/
│
├── services/
│   ├── order-service/
│   └── payment-service/
│
├── knowledge/
│   ├── architecture/
│   ├── runbooks/
│   └── postmortems/
│
├── evals/
│
├── docs/
│   ├── PRD.md
│   ├── TECH_DESIGN.md
│   └── IMPLEMENTATION_PLAN.md
│
├── tests/
│
├── docker-compose.yml
├── AGENTS.md
└── README.md
```

Backend 内部再按职责拆分：

```text
backend/
├── api/
├── agent/
├── rag/
├── tools/
├── adapters/
├── incidents/
├── approvals/
├── verification/
├── db/
└── observability/
```

这里强调：

> 按业务模块拆，不按“所有 model 一个目录、所有 service 一个目录”的方式无脑横切。

---

# 33. 模块依赖方向

推荐依赖方向：

```text
API
 ↓
Application Services
 ↓
Domain / Workflow
 ↓
Ports / Interfaces
 ↓
Adapters / Infrastructure
```

Agent 可以调用：

```text
RAG
Tools
Incident Service
```

但：

```text
RAG
Tool Adapter
Fault Lab
```

不应该反向依赖 LangGraph。

---

# 34. 关键技术约束

Codex 实现时必须遵守：

### Agent

* 单 Agent；
* 显式状态；
* Tool 白名单；
* 最大调查轮次；
* 不允许任意工具执行。

### RAG

* PostgreSQL + pgvector；
* Metadata Filter；
* Hybrid Retrieval；
* Citation。

### Side Effects

* 只允许 rollback；
* 只允许 local / staging；
* 必须 Policy Gate；
* 必须 Human Approval。

### Verification

* 执行成功 ≠ Incident 成功；
* 必须重新采集证据。

### Eval

* Fixture 可重复；
* Fault Lab 可 reset；
* 结果自动评分。

---

# 35. 当前明确不引入的技术

V0 不使用：

```text
Kubernetes
Redis
Celery
Kafka
RabbitMQ
Elasticsearch
OpenSearch
Neo4j
Multi-Agent Framework
Jira SDK
GitHub App
```

除非后续出现明确、无法通过现有架构解决的需求。

原则：

> 不因为“企业项目经常使用”就增加基础设施。

---

# 36. 关键架构决策总结

## ADR-001 PostgreSQL + pgvector

选择：

```text
PostgreSQL + pgvector
```

同时承载：

* 业务数据；
* Knowledge Metadata；
* Vector；
* Full-Text Search；
* Eval 数据；
* Workflow 持久化。

理由：

* 工程真实性较高；
* 避免同时维护多个数据库；
* 支持 Metadata Filter；
* 支持 Vector + Keyword Hybrid Retrieval；
* 后期可以继续扩展。

---

## ADR-002 单 Agent + 显式 LangGraph

不采用 Multi-Agent。

原因：

DevSupport Agent 的复杂度主要来自：

```text
状态
工具
证据
审批
验证
```

不是来自多个角色之间协作。

---

## ADR-003 Tool Layer 隔离基础设施

Agent 不直接访问具体 Observability Backend。

全部通过 Tool + Adapter。

目的：

* 可测试；
* 可替换；
* 可 Eval；
* 更安全；
* 更容易解释。

---

## ADR-004 Approval 必须在代码和 Workflow 层实现

不通过 Prompt 实现：

```text
“执行前请征求用户同意”
```

而是：

```text
Policy
+
LangGraph Interrupt
+
External Approval API
```

---

## ADR-005 Recovery Verification 独立于 Action

任何副作用 Tool：

```text
success
```

只表示：

> 操作命令执行成功。

不表示：

> Incident 已解决。

Incident 是否 RESOLVED 由 Verification 决定。

---

# 37. V0 核心系统边界

最终必须一直记住：

```text
              DevSupport Agent
┌────────────────────────────────────┐
│                                    │
│ Incident                           │
│ Agent Workflow                     │
│ RAG                                │
│ Tools                              │
│ Hypothesis / Evidence              │
│ Approval                           │
│ Verification                       │
│ Report                             │
│ Eval                               │
│                                    │
└────────────────┬───────────────────┘
                 │
        调查和操作的对象
                 ↓
┌────────────────────────────────────┐
│              Fault Lab             │
│                                    │
│ order-service                      │
│ payment-service                    │
│ logs / metrics / traces            │
│ deployment state                   │
│ fault injection                    │
│ reset                              │
└────────────────────────────────────┘
```

Fault Lab 是：

> 被调查系统。

DevSupport Agent 是：

> 调查系统。

两者必须保持概念和代码边界。

否则很容易为了让 Agent “调查成功”，把故障正确答案直接泄露给 Agent。

---

# 38. Technical Success Criteria

技术设计成功落地后，应满足：

```text
用户创建 Incident
        ↓
FastAPI 持久化 Incident
        ↓
启动 LangGraph Thread
        ↓
Agent 通过 RAG 获取领域知识
        ↓
Agent 通过 Tool Adapter 获取运行证据
        ↓
Hypothesis 随证据更新
        ↓
形成 Resolution Proposal
        ↓
Policy Gate
        ↓
LangGraph Interrupt
        ↓
用户 Approval
        ↓
Resume Thread
        ↓
执行受控 Rollback
        ↓
独立 Recovery Verification
        ↓
生成 Report
        ↓
Eval 可以重新执行同一 Scenario
```

整条链路中：

* 没有故障根因硬编码进 Agent Prompt；
* 没有 Agent 绕过 Tool Layer；
* 没有 LLM 自己批准高风险动作；
* 没有因为 Tool 返回 success 就宣布恢复；
* 所有重要行为均可追踪。
