# DevSupport Agent V0 — Implementation Plan

> Status: Ready for Execution
> Related:
>
> * `docs/PRD.md`
> * `docs/TECH_DESIGN.md`
>
> Target: 5 个高强度开发日，约 35～50 有效工时
> Goal: 完成首个可写入简历、可演示、可评测的正式 V0

---

# 1. 实施原则

## 1.1 最终目标

V0 最终必须稳定跑通：

```text
人工创建 Incident
→ RAG 检索知识
→ 生成候选故障假设
→ 调用 Logs / Metrics / Trace / Deployment Tools
→ 根据证据更新和淘汰假设
→ 输出处置方案
→ Human Approval
→ Rollback
→ Recovery Verification
→ Final Report
```

---

## 1.2 开发策略

采用：

> Vertical Slice + Incremental Integration

即：

不要先把所有数据库写完，再写所有 RAG，再写所有 Agent，最后一天才第一次集成。

而应该尽早形成：

```text
真实故障
→ Tool 能查
→ Agent 能用
→ Workflow 能跑
```

然后不断扩展。

---

## 1.3 每阶段完成定义

每个阶段必须同时满足：

```text
代码存在
+
测试通过
+
实际运行成功
+
产生可观察结果
+
没有突破 PRD Scope
```

只有 Codex 回复：

> “已实现。”

不算完成。

---

# 2. 开发角色

## ChatGPT

作为：

> Architect / Tech Lead

负责：

* 范围控制；
* 技术方案；
* 当前阶段任务说明；
* Codex Prompt；
* 验收标准；
* 代码与设计 Review；
* 出现问题时判断改设计还是改实现。

---

## Codex

作为：

> Implementation Engineer

负责：

* 阅读仓库；
* 阅读项目文档；
* 修改代码；
* 写测试；
* 执行验证；
* 检查 Git Diff；
* 汇报实现结果和风险。

Codex 不负责：

* 自行增加产品需求；
* 自行重新设计项目定位；
* 自行引入大规模新基础设施。

---

## Project Owner

负责：

* 确认产品方向；
* 启动任务；
* 本地运行；
* 查看实际页面和行为；
* 将 Codex 执行结果反馈给 ChatGPT；
* 理解关键实现。

---

# 3. 总体里程碑

```text
DAY 1
Fault Lab + Repository Foundation

DAY 2
Backend + RAG + Investigation Tools

DAY 3
LangGraph Investigation Agent

DAY 4
Approval + Rollback + Verification + Web

DAY 5
Eval + Hardening + Documentation
```

完成顺序不得随意颠倒。

特别禁止：

```text
先花大量时间做前端
```

以及：

```text
先把所有高级 RAG 功能写完
```

主调查链路优先。

---

# 4. Day 1 — 建立真实故障世界

## 目标

一天结束时必须证明：

> DevSupport Agent 已经拥有一个真实、可重复、可观测的外部微服务系统可供调查。

这一天主要建设：

```text
Fault Lab
```

而不是 Agent。

---

# 5. Task 1.1 — 初始化仓库

建立基本结构：

```text
devsupport-agent/
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
├── tests/
├── docs/
│   ├── PRD.md
│   ├── TECH_DESIGN.md
│   └── IMPLEMENTATION_PLAN.md
│
├── AGENTS.md
├── docker-compose.yml
└── README.md
```

此阶段 README 只需要基本项目说明。

---

## 完成标准

* Python Backend 有基本项目结构；
* 两个实验服务存在；
* Docker Compose 文件存在；
* `.env.example` 存在；
* `.gitignore` 正确；
* 基础测试可以运行。

---

## 禁止

不要：

* 建复杂 CI；
* 做 GitHub Actions；
* 做正式 UI；
* 建大量空模块；
* 提前实现未来功能。

---

# 6. Task 1.2 — 搭建 payment-service

实现最小 payment API。

例如：

```text
POST /payments
GET /health
```

正常状态：

```text
POST /payments
→ 返回成功
```

服务必须：

* 能独立运行；
* 有结构化日志；
* 有基本请求耗时记录；
* 支持 Trace Context；
* 有健康检查。

---

# 7. Task 1.3 — 搭建 order-service

至少实现：

```text
POST /orders
GET /health
```

正常链路：

```text
Client
↓
order-service
↓
payment-service
↓
success
```

要求：

* 必须是真实 HTTP 调用；
* 不是在同一个进程里直接调用函数；
* Trace 必须能够跨服务关联。

---

# 8. Task 1.4 — 实现故障 Scenario A

故障：

> Missing Configuration

例如：

```text
新版本 order-service
需要 PAYMENT_TIMEOUT

staging 没有配置
↓
POST /orders
↓
500
```

必须存在：

```text
inject
reset
```

机制。

不要求人工修改源码才能切换故障。

---

## 注入后必须产生

### Logs

出现明确但不过度泄漏答案的错误信息。

例如：

```text
configuration lookup failed
```

或具体异常。

### Metrics

至少：

```text
request_count ↑
error_count ↑
```

### Trace

请求失败 Trace。

### Deployment

当前运行版本能够查询。

---

# 9. Task 1.5 — 实现故障 Scenario B

故障：

> payment-service timeout

流程：

```text
payment-service latency ↑
↓
order-service waits
↓
request latency ↑
↓
timeout / failure
```

Trace 应能够明确展示：

```text
order-service
    ↓
payment-service
```

大量耗时集中于下游调用。

---

# 10. Task 1.6 — 建立 Deployment State

至少能够查询：

```text
service
current_version
previous_version
deployed_at
```

Scenario A 必须存在：

```text
healthy version
faulty version
```

切换关系。

---

# 11. Day 1 验收

必须手工验证：

## Normal

```text
POST /orders
→ Success
```

## Scenario A

```text
inject missing_config
→ POST /orders
→ Failed
→ Logs 有异常
→ Metrics 有变化
→ Trace 有异常
```

## Reset

```text
reset
→ POST /orders
→ Success
```

## Scenario B

```text
inject payment_timeout
→ 请求明显变慢 / 失败
→ Trace 明确显示 payment-service 耗时
```

---

# 12. Day 1 最终成果

必须能够回答：

> 如果暂时没有 Agent，人类工程师是否可以通过这些 Logs / Metrics / Trace 找到两个故障？

如果答案是否定的，不进入 Day 2。

---

# 13. Day 2 — Backend、RAG 和调查工具

## 目标

一天结束时：

> 系统可以通过稳定的结构化 Tool 查询真实故障环境，并能从知识库检索相关 Runbook。

---

# 14. Task 2.1 — PostgreSQL 基础设施

加入：

```text
PostgreSQL
+
pgvector
```

建立数据库连接、Migration 和最小数据模型。

第一批业务实体：

```text
Incident
Hypothesis
Evidence
ToolCall
Approval
Action
Verification
Report
```

知识相关：

```text
KnowledgeDocument
KnowledgeChunk
```

暂时不追求复杂 Schema。

---

# 15. Task 2.2 — Incident API

至少实现：

```text
POST /incidents
GET /incidents/{id}
GET /incidents
```

创建 Incident 时保存：

```text
service
environment
time_range
description
status
```

初始状态：

```text
OPEN
```

---

# 16. Task 2.3 — 准备知识材料

准备约：

```text
10～15 份 Markdown
```

至少包含：

### Architecture

* 系统总体架构；
* order-service 说明；
* payment-service 说明。

### Runbook

至少：

* order-service 500 排查；
* 发布后异常排查；
* 下游超时排查。

### Postmortem

至少若干：

* 历史配置缺失；
* 历史 payment timeout；
* 若干干扰事故。

重要：

> 不要把每个当前 Eval 的最终答案直接原样写在唯一一篇文档里。

知识库需要存在一定干扰信息。

---

# 17. Task 2.4 — 文档导入 Pipeline

实现：

```text
Markdown
↓
Parse
↓
Chunk
↓
Metadata
↓
Embedding
↓
PostgreSQL
```

Metadata 至少：

```text
service
environment
document_type
source
```

---

# 18. Task 2.5 — Hybrid Retrieval

实现：

```text
Vector Search
+
PostgreSQL Full-Text Search
+
简单融合
```

支持：

```text
service
environment
document_type
```

过滤。

返回：

```text
chunk_id
document
content
metadata
scores
citation
```

---

## V0 不阻塞项

Reranker 如果接入简单：

```text
加入
```

如果明显影响主链路：

```text
记录为 V1
```

不得为了 Reranker 延迟 Day 3。

---

# 19. Task 2.6 — Tool Schemas

所有 Tool 使用结构化模型。

完成：

```text
search_knowledge
query_logs
query_metrics
query_traces
get_deployment_history
rollback_deployment
```

Day 2 暂不要求 Agent 使用这些 Tool。

但 Tool 自身必须独立可测试。

---

# 20. Task 2.7 — query_logs

支持：

```text
service
time_range
level
```

返回摘要，例如：

```text
total_matches
patterns
first_seen
last_seen
sample_events
trace_ids
```

不得直接返回无限日志文本。

---

# 21. Task 2.8 — query_metrics

至少能够获得：

```text
request_count
error_count
error_rate
request_latency
health-related signal
```

---

# 22. Task 2.9 — query_traces

至少返回：

```text
trace_id
service spans
duration
errors
slowest span
```

Scenario B 应可以通过结果看出：

```text
payment-service
```

是主要耗时来源。

---

# 23. Task 2.10 — get_deployment_history

至少返回：

```text
service
current_version
previous_version
deployed_at
```

---

# 24. Day 2 测试

至少包含：

### RAG

给定：

```text
order-service 发布后返回 500
```

能检索到相关：

```text
Runbook / Postmortem
```

### Logs Tool

Scenario A 下能够返回配置相关错误模式。

### Trace Tool

Scenario B 下能够返回：

```text
payment-service
```

明显慢于其他 Span。

### Deployment Tool

Scenario A 下能够返回最近发布。

---

# 25. Day 2 验收

必须做到：

```text
不通过 Agent
```

直接调用 Tool 就可以完成一次人工调查。

即：

```text
search knowledge
+
deployment
+
logs
+
metrics
+
traces
```

共同提供足够证据。

---

# 26. Day 3 — LangGraph 调查 Agent

## 目标

一天结束时：

> Agent 能够自主调查两个故障，不需要按硬编码固定顺序调用所有工具。

Day 3 是项目最核心的一天。

---

# 27. Task 3.1 — 定义 Agent State

至少包含：

```text
incident
current_stage
hypotheses
evidence
current_goal
tool_history
investigation_round
proposed_action
final_conclusion
```

State 不能塞大量完整原始日志。

主要保存：

```text
结构化结果
+
Evidence references
```

---

# 28. Task 3.2 — Intake Node

实现：

```text
Incident
↓
Structured Incident
```

判断：

* service；
* environment；
* time range；
* symptoms。

缺少必要字段：

```text
NEEDS_INFORMATION
```

---

# 29. Task 3.3 — Retrieval Node

调用：

```text
search_knowledge
```

检索：

* Runbook；
* Service Docs；
* Historical Incident。

结果转化为 Evidence。

---

# 30. Task 3.4 — Hypothesis Generation

生成：

```text
2～4
```

个候选假设。

示例：

```text
H1 Deployment configuration issue
H2 payment-service timeout
H3 order-service internal failure
```

必须是结构化输出。

---

# 31. Task 3.5 — Investigation Planner

输入：

```text
Incident
Hypotheses
Existing Evidence
Tool History
```

输出：

```text
investigation_goal
tool_name
tool_arguments
reason
```

Tool 只能从白名单选择。

---

# 32. Task 3.6 — Tool Execution

执行：

```text
selected_tool
```

Tool Result：

```text
→ Evidence
```

并记录 Tool Call。

---

# 33. Task 3.7 — Hypothesis Update

更新：

```text
supporting_evidence
contradicting_evidence
confidence
status
next_check
```

状态：

```text
ACTIVE
SUPPORTED
REJECTED
CONFIRMED
```

---

# 34. Task 3.8 — Investigation Loop

实现：

```text
plan
→ tool
→ evidence
→ update
→ evaluate
```

循环。

必须存在：

```text
max_rounds
max_tool_calls
```

防止无限循环。

---

# 35. Task 3.9 — Evidence Evaluation

结果：

```text
CONTINUE
CONCLUDE
NEEDS_MANUAL_ACTION
```

只有有足够 Evidence 才能：

```text
CONCLUDE
```

---

# 36. Task 3.10 — Resolution Proposal

结构化输出：

```text
root_cause
confidence
recommended_action
reason
supporting_evidence_ids
risk
```

---

# 37. Day 3 最关键测试

## Scenario A

Agent 最终应：

```text
确认 order-service 发布 / 配置相关问题
```

并：

```text
建议 rollback
```

---

## Scenario B

Agent 应：

```text
识别 payment-service timeout
```

且：

```text
不得建议 rollback order-service
```

---

# 38. Day 3 反作弊检查

检查 Prompt 和 Fixture。

禁止：

```text
if scenario == missing_config:
    root_cause = ...
```

禁止把：

```text
正确根因
```

直接写进 Agent Prompt。

禁止：

```text
固定调用：
deployment
→ logs
→ metrics
→ traces
```

无论情况如何都跑完整顺序。

Agent 必须至少体现一定工具选择差异。

---

# 39. Day 3 验收

CLI / API 层即可。

暂时不要求漂亮前端。

必须能够看到：

```text
Incident
Hypothesis
Tool Calls
Evidence
Final Conclusion
```

完整变化过程。

---

# 40. Day 4 — Human Approval、Rollback、Recovery Verification、Web

## 目标

一天结束时：

> Scenario A 能完成从 Incident 创建一直到 RESOLVED 的完整闭环。

---

# 41. Task 4.0 — Persistent Workflow Foundation

这是 Day 4 其余任务的前置条件，必须先完成并验收。

LangGraph 必须使用持久化 Checkpointer；V0 使用 PostgreSQL。每个 Incident 创建时必须固定并持久化一个 `thread_id`，Workflow 启动、暂停、查询和恢复都使用该同一 `thread_id`。

必须验证：

```text
start workflow
↓
interrupt / pause
↓
按相同 thread_id resume
```

恢复后必须保留已有的：

```text
Hypotheses
Evidence
Tool History
Policy / Approval Context
```

Human Approval 依赖这一基础。不得通过重新启动调查、创建新 Thread 或重跑 Day 3 investigation 来模拟 resume。

---

# 42. Task 4.1 — Policy Gate

在执行 Action 前检查：

```text
environment
action
service
```

Policy 必须由代码执行，不能只靠 Prompt。LLM 的 `ProposedAction` 仅是 high-level recommendation，不授予执行权限，也不能生成或授权 `service`、`target_version`、`current_version` 等执行参数。

Policy Gate 必须依据真实 Evidence 和当前 Deployment State 生成并校验可执行 Action 参数；事实缺失、矛盾或无法验证时必须 DENIED。

固定规则：

```text
production → DENIED
unsupported environment → DENIED
local + rollback + supported service + verified deployment facts → APPROVAL_REQUIRED
```

当前 Fault Lab 实际只支持 `local`，因此只有 `local` 可进入可执行路径；不得因为 environment 非 production 而默认放行。`rollback_deployment` 必须始终 approval required。

Policy Gate 之外的 Action 不得执行。

---

# 43. Task 4.2 — Human Approval

实现 LangGraph Interrupt。

流程：

```text
propose rollback
↓
WAITING_APPROVAL
↓
interrupt
```

Approval API：

```text
POST /incidents/{id}/approval
```

输入：

```text
APPROVE
REJECT
```

---

# 44. Task 4.3 — Workflow Resume

批准后：

```text
使用 Task 4.0 持久化的相同 thread_id 恢复相同 thread
```

不得：

```text
重新启动一个新的调查任务
```

必须保留之前：

```text
Hypotheses
Evidence
Tool History
```

---

# 45. Task 4.4 — rollback_deployment

`rollback_deployment` 是独立 remediation execution path，不加入或复用 Day 3 的 read-only Investigation Planner / Tool Executor。它只接收 Policy Gate 根据真实 Evidence、Deployment State 和 Approval Record 生成的 Action 参数。

执行真正的 Fault Lab 运行状态变化。

Scenario A：

```text
faulty version
↓ rollback
healthy version
```

必须导致：

```text
POST /orders
```

真实恢复。

不能只修改数据库版本号，也不得复用 Fault Lab reset。Rollback 不得清理历史 logs、traces 或 metrics。

---

# 46. Task 4.5 — Recovery Verification

执行 Rollback 后：

使用 action 后重新采集的新 Evidence 验证：

```text
deployment state
health
core request
new error signal / critical logs
metrics delta / post-action signal
```

Metrics 不要求累计 `error_count` 清零；判断的是 action 后新增请求的错误信号和趋势。Tool success != Incident resolved。

结果：

```text
PASS
FAIL
INCONCLUSIVE
```

---

## PASS

```text
RESOLVED
```

## FAIL

不能：

```text
RESOLVED
```

NEEDS_MANUAL_ACTION
```

V0 中 FAIL / INCONCLUSIVE 不自动连续执行第二次 remediation。

---

# 47. Task 4.6 — Final Report

生成：

```text
Incident Summary
Timeline
Root Cause
Hypotheses
Key Evidence
Action
Approval
Execution
Verification
Final Status
```

所有重要结论关联 Evidence ID。

---

# 48. Task 4.7 — Web Console

V0 Web 优先：

> 功能完整，而不是 UI 漂亮。

至少实现：

### Incident Creation

输入：

```text
service
environment
time range
description
```

### Investigation View

展示：

```text
Incident Status
Current Stage
Hypotheses
Evidence
Tool Timeline
```

### Approval

```text
Approve
Reject
```

### Final Report

展示最终调查结果。

---

# 49. 前端更新方式

优先：

```text
简单轮询
```

如果 SSE 接入非常直接可以使用 SSE。

禁止：

为了实时动画花费大量时间。

---

# 50. Day 4 完整验收 Demo

必须稳定运行：

```text
1 Inject missing_config

2 Create Incident

3 Agent Starts Investigation

4 RAG Retrieval

5 Deployment / Logs / Metrics / Traces

6 Hypothesis Updated

7 Root Cause Confirmed

8 Agent Proposes Rollback

9 Status = WAITING_APPROVAL

10 User Approves

11 Same Workflow Resumes

12 Rollback Executes

13 Recovery Verification Runs

14 POST /orders Works

15 Status = RESOLVED

16 Final Report Generated
```

如果这条链路没有稳定跑通：

> 不进入 UI 优化。

最小 E2E 验收还必须覆盖：

```text
Scenario A: Approve → same thread resume → rollback → verification PASS → RESOLVED
Approval Reject → no rollback
production / unsupported environment → Policy DENIED
Scenario B → 不得错误 rollback
rollback success + verification FAIL / INCONCLUSIVE → NEEDS_MANUAL_ACTION，且不得 RESOLVED
```

---

# 51. Day 5 — Eval、Hardening 与项目包装

## 目标

将：

```text
能演示
```

升级为：

```text
可重复
可测试
可量化
可写简历
```

---

# 52. Task 5.1 — Eval Fixture Format

每条 Fixture 至少定义：

```text
id
scenario
fault_config
incident_input
expected_root_cause
required_evidence
acceptable_tools
forbidden_actions
approval_behavior
expected_final_status
```

---

# 53. Task 5.2 — 第一批 Eval

目标：

```text
8～12
```

条。

至少包括：

### Scenario A

不同：

* 描述方式；
* 时间范围；
* 干扰信息。

### Scenario B

不同：

* 描述方式；
* timeout 程度；
* 故障表现。

### Safety / Workflow

包括：

* production 环境；
* Approval Reject；
* Tool Failure；
* 缺失 Input；
* Verification Failure。

---

# 54. Task 5.3 — Eval Runner

自动执行：

```text
Reset
↓
Inject
↓
Create Incident
↓
Run Workflow
↓
Handle Approval
↓
Collect Result
↓
Score
```

不得每条任务人工点击页面运行。

---

# 55. Task 5.4 — 指标

V0 至少输出：

## Agent

```text
Root Cause Accuracy
Key Evidence Recall
Tool Selection Accuracy
Task Completion Rate
```

## Safety

```text
Approval Trigger Accuracy
Unauthorized Execution Count
```

## Efficiency

```text
Average Tool Calls
Average Latency
Token Usage
```

如果 Token 获取困难：

可以暂时记录：

```text
LLM Call Count
```

但不能伪造 Token 数字。

---

# 56. Task 5.5 — 对照实验

优先完成：

```text
Direct LLM
vs
Full Agent
```

如果时间允许：

增加：

```text
RAG Only
```

目标不是证明 Agent 一定全指标更高。

目标是：

> 有真实实验数据可以分析。

---

# 57. Task 5.6 — Integration Tests

重点测试：

```text
Incident Creation
RAG Retrieval
Tool Schemas
Tool Failure
Investigation Routing
Approval Gate
Approval Resume
Rollback
Verification
Eval Runner
```

---

# 58. Task 5.7 — Failure Hardening

重点处理最容易影响 Demo 的问题：

* LLM JSON 不合法；
* Tool 参数错误；
* Tool 超时；
* Agent 循环；
* Workflow Resume 失败；
* 数据库状态不一致；
* Fault Reset 不彻底；
* Verification 假阳性；
* Docker 启动顺序。

不要求解决所有生产级 Edge Cases。

---

# 59. Task 5.8 — README

README 必须包含：

```text
Project Overview
Problem
Architecture
Agent Workflow
RAG
Tool System
Fault Lab
Human Approval
Recovery Verification
Eval
Quick Start
Demo
Results
Known Limitations
Roadmap
```

---

# 60. Task 5.9 — 项目图

至少准备两张：

## Architecture

```text
Web
→ FastAPI
→ LangGraph
→ RAG / Tools
→ PostgreSQL / Fault Lab
```

## Agent Workflow

```text
Incident
→ Retrieval
→ Hypothesis
→ Investigation
→ Approval
→ Execution
→ Verification
```

---

# 61. Task 5.10 — 简历数据

只有 Eval 跑完以后才填写：

```text
Root Cause Accuracy
Task Completion Rate
Tool Selection Accuracy
```

禁止在代码完成前预填数字。

---

# 62. V0 Release Gate

V0 Release 前必须满足：

## Core Business

* [ ] 人工创建 Incident；
* [ ] Agent 自动调查；
* [ ] RAG 是调查的一部分；
* [ ] Agent 实际调用多个 Runtime Tools；
* [ ] Hypothesis 根据 Evidence 变化；
* [ ] Scenario A 正确建议 Rollback；
* [ ] Scenario B 不错误回滚 order-service。

## Human Control

* [ ] Rollback 必须审批；
* [ ] Reject 后不执行；
* [ ] Approve 后原 Workflow 恢复；
* [ ] Production 被代码层禁止。

## Verification

* [ ] Rollback 后真正恢复服务；
* [ ] 系统重新采集证据；
* [ ] Verification Failure 不进入 RESOLVED。

## Eval

* [ ] 至少 8 条 Fixture；
* [ ] 自动运行；
* [ ] 自动评分；
* [ ] 输出真实指标。

## Engineering

* [ ] Docker Compose 能启动主要系统；
* [ ] 基础测试通过；
* [ ] 无 Secret；
* [ ] README 可使用；
* [ ] 有已知限制说明。

---

# 63. Git 提交策略

每一个阶段形成独立可审查提交。

推荐：

```text
chore: initialize devsupport agent repository

feat: add fault lab microservices

feat: add reproducible fault scenarios

feat: add incident persistence and api

feat: add hybrid knowledge retrieval

feat: add investigation tools

feat: add langgraph investigation workflow

feat: add hypothesis evidence loop

feat: add human approval workflow

feat: add controlled rollback

feat: add recovery verification

feat: add incident investigation console

test: add agent evaluation fixtures

docs: add v0 evaluation results and demo
```

不要：

```text
git commit -m "finish project"
```

提交几千个文件。

---

# 64. Codex 每个任务的标准工作模式

每次只交给 Codex 一个明确阶段。

Prompt 结构统一：

```text
你正在实现 DevSupport Agent V0。

开始前读取：

AGENTS.md
docs/PRD.md
docs/TECH_DESIGN.md
docs/IMPLEMENTATION_PLAN.md

当前只实现：

[任务]

要求：

[具体要求]

明确不做：

[Scope]

完成后：

1. 运行相关测试；
2. 运行 lint / type checks；
3. 检查 git diff；
4. 汇报修改文件；
5. 汇报测试结果；
6. 汇报剩余问题；
7. 不要自动进入下一阶段。
```

---

# 65. Codex 完成后的标准回报

每个阶段要求 Codex 返回：

```text
Implemented
- ...

Tests
- command
- result

Manual Verification
- ...

Files Changed
- ...

Known Issues
- ...

Scope Check
- 是否实现了范围外内容

Ready for Review
```

然后由 ChatGPT Review。

---

# 66. 不允许的 Vibe Coding 模式

禁止：

```text
“根据 PRD 把整个项目实现出来。”
```

也禁止一次性：

```text
让 Codex 工作几十分钟
→ 修改整个仓库
→ 不看 diff
→ 继续下一需求
```

推荐粒度：

```text
1～3 小时一个工程任务
```

复杂任务可以进一步拆分。

---

# 67. 时间预算

建议：

| 阶段                          |   有效工时 |
| --------------------------- | -----: |
| Day 1 Fault Lab             |   7～9h |
| Day 2 Backend / RAG / Tools |  8～10h |
| Day 3 Agent Workflow        |  8～10h |
| Day 4 HITL / Verify / Web   |  7～10h |
| Day 5 Eval / Hardening      |  7～10h |
| 总计                          | 37～49h |

---

# 68. 时间不足时的降级顺序

如果开发出现延期，按以下顺序降级。

## 第一优先级删除

```text
Reranker
SSE
复杂 UI
额外图表
RAG-only 对照实验
```

## 第二优先级简化

```text
Metrics 查询种类
Trace UI
额外知识文档
额外 Eval Fixture
```

## 不允许删除

```text
两个真实 Fault Scenario
RAG
Tool Calling
Hypothesis / Evidence
Human Approval
Rollback
Recovery Verification
至少 8 条 Eval
```

这些是 V0 的求职价值核心。

---

# 69. Definition of Done

DevSupport Agent V0 的真正 Definition of Done：

不是：

```text
所有计划代码都写出来了。
```

而是：

```text
一次真实故障
↓
Agent 根据外部证据调查
↓
形成并更新假设
↓
选择正确行动
↓
人工控制副作用
↓
系统真正发生变化
↓
Agent 重新验证
↓
任务正确结束
↓
整个过程能够重复评测
```

只有这条链路稳定成立，V0 才正式完成。
