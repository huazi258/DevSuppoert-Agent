# DevSupport Agent V0 — Web Console Design

## 1. Goal

Task 4.7 的目标是把已经完成并验收的后端 Incident 调查闭环暴露为一个可操作、可演示的 Web Console。

V0 Web 优先：

> 功能完整、状态真实、操作安全。

不追求复杂视觉效果。

完整用户路径：

```text
Create Incident
→ Start Investigation
→ Observe Investigation
→ WAITING_APPROVAL
→ Approve / Reject
→ Observe Resume / Remediation / Verification
→ Final Status
→ Final Report
```

Web 不拥有 Agent 业务逻辑。

## 2. Scope

Task 4.7 包含三个子任务：

```text
4.7.0 Web API Surface
4.7.1 Web Console UI
4.7.2 Web E2E & Polish
```

每个子任务独立 commit、独立 Review。

## 3. Architecture

采用：

```text
Next.js Web
    ↓ REST
FastAPI
    ↓
PostgreSQL + LangGraph Checkpoint
```

前端只访问 FastAPI。前端不得直接访问 PostgreSQL、LangGraph checkpointer、Fault Lab 或 LLM，也不得自行推导审批或 remediation 状态。

## 4. Web API Strategy

使用专用 Workflow API，不把完整 AgentState 原样暴露为公共 API。

### Existing APIs

继续复用：

```text
POST /incidents
GET  /incidents
GET  /incidents/{id}
POST /incidents/{id}/approval
GET  /incidents/{id}/report
```

## 5. Start Workflow API

新增：

```text
POST /incidents/{incident_id}/workflow
```

职责：读取 exact Incident，使用 Incident 已持久化的 `thread_id`，并启动 production investigation graph。

必须使用已经验收的正式 workflow composition。不得创建新 `thread_id`、启动第二个 investigation thread、绕过 production graph 或修改 Agent 算法。

### Start Preconditions

只允许尚未启动 workflow 的合法 Incident。至少拒绝：

```text
unknown Incident
missing thread_id
已经存在 workflow checkpoint
terminal Incident
WAITING_APPROVAL / REMEDIATING / VERIFYING
```

重复 start 必须 fail closed，不得重新调查。HTTP conflict 使用 `409`。

## 6. Workflow Read API

新增：

```text
GET /incidents/{incident_id}/workflow
```

它是只读 Web projection，不得直接返回内部 AgentState JSON。返回严格 Pydantic response，至少包含：

```text
incident_id
incident_status
current_stage
hypotheses
evidence
tool_history
current_goal
final_conclusion
proposed_action
policy_outcome
approval_outcome
execution_outcome
verification_outcome
report_outcome
```

## 7. Hypothesis Projection

每条至少包含：

```text
id
summary
status
confidence
supporting_evidence_ids
contradicting_evidence_ids
next_check
```

## 8. Evidence Projection

每条至少包含：

```text
id
evidence_type
source
summary
reference
```

V0 Web 默认不展示完整 `Evidence.data`，以避免大 payload 和与内部 Tool 数据结构绑定；后续需要时再增加详情能力。

## 9. Tool Timeline Projection

由 checkpoint 的 `tool_history` 投影。每条至少包含：

```text
tool_name
tool_arguments
status
duration_ms
evidence_ids
error
```

Tool Timeline 只是调查审计视图，不得重新执行 Tool。

## 10. Approval

继续复用：

```text
POST /incidents/{id}/approval
```

Web 只允许 `APPROVE` 与 `REJECT`。Web 不允许用户修改 `service`、`environment`、`current_version`、`target_version` 或任何 Action parameters。审批 UI 只显示后端已经确定的 Action。

## 11. Polling

V0 使用简单轮询，默认每 `2–3 seconds` 读取：

```text
GET /incidents/{id}
GET /incidents/{id}/workflow
```

进入 `RESOLVED` 或 `NEEDS_MANUAL_ACTION` 后停止 workflow polling。进入 `WAITING_APPROVAL` 时继续低频读取状态，但 UI 显示 Approval controls。

不引入 SSE/WebSocket，除非实现中证明轮询无法满足 V0。

## 12. Final Report

当 `report_outcome != null` 或 Incident terminal 后，前端读取：

```text
GET /incidents/{id}/report
```

展示：

```text
Incident Summary
Timeline
Root Cause
Hypotheses
Key Evidence
Recommended Action
Action
Approval
Execution
Verification
Final Status
```

Web 只渲染 persisted Final Report，不得重新生成结论。

## 13. Web Pages

V0 保持极简页面结构：

```text
/
└─ Incident List + Create Incident

/incidents/{id}
└─ Incident Console
```

不需要额外 Dashboard、Settings、Authentication 页面。

## 14. Incident List

展示：

```text
service
environment
status
created_at
```

支持打开 Incident Console。不做 search、pagination、filter builder 或 bulk actions，除非数据量实际影响使用。

## 15. Incident Creation

表单：

```text
service
environment
time_range_start
time_range_end
description
```

创建成功后导航至 `/incidents/{id}`。Incident Console 提供明确的 `Start Investigation` 按钮。

不在 `POST /incidents` 时隐式启动 Agent，以保留 `create` 与 `workflow start` 两个明确边界。

## 16. Incident Console Layout

V0 页面按信息优先级组织。

### Header

```text
Incident ID
Service
Environment
Incident Status
Current Stage
```

### Investigation

```text
Hypotheses
Evidence
Tool Timeline
```

### Decision / Action

按状态显示：

```text
Proposed Action
Policy Decision
Approval controls
Execution
Verification
```

### Final

```text
Final Conclusion
Final Report
```

不需要复杂图表。

## 17. Status UX

必须清晰区分：

```text
OPEN
INVESTIGATING
WAITING_APPROVAL
REMEDIATING
VERIFYING
RESOLVED
NEEDS_MANUAL_ACTION
```

以及 Agent current stage。Incident Status 与 Agent Stage 不得混成同一个字段。

## 18. Loading / Error / Empty States

至少处理：

```text
backend unavailable
workflow not started
workflow loading
report not generated
approval request failed
workflow resume failed
404
409
503
```

错误信息优先展示 FastAPI `detail`，不得静默失败。

## 19. Backend Boundary for Task 4.7

4.7 允许新增：

```text
workflow start orchestration endpoint
workflow read projection endpoint
strict Web response schemas
必要的 dependency factory
CORS/local frontend configuration
```

禁止修改：

```text
RAG algorithm
Hypothesis logic
Planner
Tool selection
Policy rules
Approval safety
rollback semantics
Recovery Verification rules
Final Report generation logic
```

## 20. Frontend Boundary

使用当前：

```text
Next.js 15
React 19
TypeScript
```

不引入大型 UI framework。可以使用原生 CSS、小型组件拆分、fetch 与 React hooks，除非已有依赖能直接复用。

## 21. Task Breakdown

### Task 4.7.0 — Web API Surface

完成：

```text
POST /incidents/{id}/workflow
GET  /incidents/{id}/workflow
```

包含 strict schemas、start idempotency / conflict safety、checkpoint read projection 与 API tests。不得修改 Web UI。

### Task 4.7.1 — Web Console UI

完成：

```text
Incident List
Incident Creation
Incident Console
Start Investigation
Investigation View
Approval
Execution / Verification status
Final Report
polling
loading/error/empty states
```

不得改 Agent 核心。

### Task 4.7.2 — Web E2E & Polish

真实浏览器验证：

```text
Scenario A
Create
→ Start
→ WAITING_APPROVAL
→ Approve
→ RESOLVED
→ Final Report
```

并验证 Reject path 与 Manual-action terminal path。只修复影响演示和使用的问题，不做视觉重构。

## 22. Definition of Done

Task 4.7 完成时，用户必须可以只通过 Web：

```text
创建 Incident
启动调查
观察 Hypothesis / Evidence / Tool Calls
看到 WAITING_APPROVAL
Approve / Reject
观察 remediation / verification
看到 RESOLVED 或 NEEDS_MANUAL_ACTION
阅读 Final Report
```

同时：

```text
Web 不拥有业务规则
Web 不绕过 Approval
Web 不直接执行 rollback
Web 不直接访问 Fault Lab
Web 不伪造 Agent 状态
```
