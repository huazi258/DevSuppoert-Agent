# Web API Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 DevSupport Agent V0 提供安全、严格、面向 Web 的 workflow start/read HTTP API，使后续 Next.js Console 可以启动正式调查并读取持久化 investigation state，而不暴露原始 LangGraph AgentState。

**Architecture:** 新增薄 `workflow_console` application/service boundary。Router 只负责 HTTP status mapping；`WorkflowConsoleService` 负责 Incident/checkpoint lifecycle 与 authoritative projection；`PostgresWorkflowRuntime` 使用现有 PostgreSQL checkpointer 和已验收的 production investigation graph。Workflow GET 返回独立 Pydantic Web projection，并从 PostgreSQL Action 记录投影审批所需的可信执行参数。

**Tech Stack:** FastAPI、Pydantic v2、SQLAlchemy 2、PostgreSQL、LangGraph PostgreSQL Checkpointer、pytest。

## Global Constraints

* 不修改 RAG algorithm、Hypothesis generation/update、Planner、Tool selection、Policy rules、Approval safety、rollback semantics、Recovery Verification 或 Final Report generation。
* 不创建新的 LangGraph thread；一个 Incident 始终使用已持久化的 `thread_id`。
* 不直接把 `AgentState` 作为 HTTP response；Workflow read 必须是只读 projection。
* Evidence Web projection 不返回完整 `Evidence.data`。
* Approval UI 所需 Action 信息必须来自 PostgreSQL persisted Action，而不是 LLM `ProposedAction.parameters`。
* Task 4.7.0 不修改 `apps/web`，最终只提交一个 implementation commit。

## File Structure

- [ ] Create `apps/backend/src/devsupport_backend/schemas/workflows.py`: strict Web workflow response contracts；不访问数据库、LangGraph，不复用 AgentState response。
- [ ] Create `apps/backend/src/devsupport_backend/workflow_console.py`: start/read application boundary、checkpoint read、production graph composition、start conflict protection、projection 和 authoritative Action binding。
- [ ] Modify `apps/backend/src/devsupport_backend/routers/incidents.py`: 两个 endpoint、HTTP mapping、dependency injection。
- [ ] Modify `apps/backend/src/devsupport_backend/main.py`: 本地 Next.js V0 CORS；不含业务逻辑。
- [ ] Create `apps/backend/tests/test_workflow_console.py`: projector/service/start conflict tests。
- [ ] Modify `apps/backend/tests/test_incidents_api.py`: workflow HTTP API、404/409/503、GET read-only、CORS tests。

## Task 1 — Strict Workflow Web Projection

- [ ] 在 `devsupport_backend.schemas.workflows` 定义下列 strict Pydantic models，全部设置 `model_config = ConfigDict(extra="forbid")`：

  ```text
  WorkflowHypothesisResponse
  WorkflowEvidenceResponse
  WorkflowToolErrorResponse
  WorkflowToolHistoryResponse
  WorkflowFinalConclusionResponse
  WorkflowProposedActionResponse
  WorkflowPolicyResponse
  WorkflowActionParametersResponse
  WorkflowActionResponse
  WorkflowApprovalResponse
  WorkflowExecutionResponse
  WorkflowVerificationResponse
  WorkflowReportOutcomeResponse
  WorkflowResponse
  ```

- [ ] `WorkflowResponse` fields：

  ```text
  incident_id, incident_status, current_stage
  hypotheses, evidence, tool_history, current_goal
  final_conclusion, proposed_action, policy_outcome, action
  approval_outcome, execution_outcome, verification_outcome, report_outcome
  ```

- [ ] `WorkflowHypothesisResponse`：`id`、`summary`、`status`、`confidence`、支持/反对 evidence IDs、`next_check`。
- [ ] `WorkflowEvidenceResponse`：`id`、`evidence_type`、`source`、`summary`、`reference`；明确排除 `data`。
- [ ] Tool error/history：`code`、`message`、`retryable`，以及 `tool_name`、`tool_arguments: dict[str, JsonValue]`、`status`、`duration_ms`、`evidence_ids`、`error`。
- [ ] Final conclusion 公开字段与当前 `FinalConclusion` 一致；Proposed Action 只暴露 `action_type`、`summary`、`reason`、`risk`、`supporting_evidence_ids`，不暴露 parameters。
- [ ] Policy response 包含 `decision`、`reason_code`、`reason`、`action_id`。
- [ ] `WorkflowActionParametersResponse` 仅为 `service`、`environment`、`current_version`、`target_version`、`reason`；Action response 还包含 action ID/type/status、`executed_at`。
- [ ] Approval、Execution、Verification、Report outcome response 分别与现有 checkpoint-safe outcomes 的公开字段精确对齐。
- [ ] `action` 表达可信链路 `PolicyOutcome.action_id → PostgreSQL Action → WorkflowActionResponse`，不得由 `ProposedAction.parameters` 推导。

## Task 2 — Pure Projection + Authoritative Action Binding

- [ ] 先在 `test_workflow_console.py` 写 failing projector tests，再实现：

  ```python
  def project_workflow_response(
      incident: Incident,
      state: AgentState,
      action: Action | None,
  ) -> WorkflowResponse:
  ```

- [ ] 验证 checkpoint Incident 的 ID、service、environment、description、time range 都与 PostgreSQL Incident 一致；否则 `WorkflowStateConflict`。
- [ ] 当 `policy_outcome is None` 时 `action must be None`。
- [ ] 当 `policy_outcome.action_id` 非空时，要求 Action 存在、ID 相等、且 `Action.incident_id == Incident.id`；否则 `WorkflowStateConflict`。
- [ ] Action 参数仅使用 `ActionExecutionParameters.model_validate(action.parameters)`。
- [ ] 忽略 `EvidenceContext.data`，所有内部 Enum 使用 `.value`。
- [ ] Happy-path tests 验证 hypotheses/evidence/tool history/final conclusion/policy 与 exact persisted Action；Evidence.data 不出现。
- [ ] Mismatch tests 验证 policy/action mismatch 与 checkpoint/Incident mismatch fail closed。

## Task 3 — Workflow Runtime Boundary

- [ ] 定义：

  ```python
  class WorkflowRuntime(Protocol):
      def get_state(self, thread_id: str) -> AgentState | None: ...
      def start(self, incident: Incident) -> AgentState: ...
  ```

- [ ] 实现 `PostgresWorkflowRuntime(session)`。
- [ ] `get_state` 使用既有 `open_postgres_checkpointer()`、`WorkflowService.config_for(thread_id)` 与最小 read-only `StateGraph(AgentState)` 读取状态；无 checkpoint 返回 None。不得创建 checkpoint、调用 LLM/Tool 或修改 Incident。
- [ ] `start` 必须复用 `build_production_investigation_graph(...)`，不得调用测试 graph 或重复 topology。
- [ ] 组合既有生产依赖：`OpenAICompatibleLLMClient.from_settings(settings)`、`OpenAICompatibleEmbeddingClient.from_settings(settings)`、`RAGService`、既有 Fault Lab tool adapters 的 `ToolExecutionDependencies`、`LLMEvidenceEvaluator`、`PolicyGateService`、`ApprovalWaitService`、`ApprovalDecisionService` 与 `InvestigationWorkflowDependencies`。
- [ ] 使用 `open_postgres_checkpointer()`、production graph 和 `WorkflowService(graph).start(incident)`；必须使用 Incident 原有 thread ID。批准后的 continuation 仍由 `PostgresApprovalWorkflowCoordinator` 负责。

## Task 4 — WorkflowConsoleService Start/Read Semantics

- [ ] 定义 `WorkflowConsoleError`、`WorkflowConflictError`、`WorkflowNotStartedError`、`WorkflowStateConflict`、`WorkflowStartError`。
- [ ] 定义：

  ```python
  class WorkflowConsoleService:
      def __init__(self, session: Session, runtime: WorkflowRuntime) -> None: ...
      def start(self, incident_id: UUID) -> WorkflowResponse: ...
      def read(self, incident_id: UUID) -> WorkflowResponse: ...
  ```

- [ ] `read` 顺序：load Incident（未知为 404 semantic）→ require non-empty thread ID → `runtime.get_state(thread_id)` → None 时 `WorkflowNotStartedError` → 从 policy action ID load authoritative Action → project。不得写入。
- [ ] `start` 必须 `select(Incident).where(Incident.id == incident_id).with_for_update()` 锁定 exact Incident，防止双击并发 start。
- [ ] 只允许 `Incident.status == OPEN`、非空合法 thread ID、且 `runtime.get_state(thread_id) is None`；否则 `WorkflowConflictError`。
- [ ] 先将 Incident status 置为 `INVESTIGATING` 并 commit，再调用 `runtime.start(incident)`；不得生成或替换 thread ID。
- [ ] runtime start 抛错后重新读取 checkpoint：若无 checkpoint，status 恢复 `OPEN`、commit、抛 `WorkflowStartError`；若已有 checkpoint，保留 checkpoint 和当前 DB status，不删除、不自动 retry，仍抛 `WorkflowStartError`。
- [ ] 成功时从 returned state 加载 authoritative Action，`session.refresh(incident)` 后 project；不得覆盖节点可能写入的 `WAITING_APPROVAL` 或 `NEEDS_MANUAL_ACTION`。

## Task 5 — HTTP Endpoints

- [ ] 在 `routers/incidents.py` 增加：

  ```python
  def get_workflow_runtime(session: SessionDependency) -> WorkflowRuntime:
      return PostgresWorkflowRuntime(session)
  ```

  并定义对应 `Annotated` dependency。

- [ ] 增加 `POST /{incident_id}/workflow` 与 `GET /{incident_id}/workflow`，response model 为 `WorkflowResponse`。
- [ ] mapping：unknown Incident 为 `404 / "Incident not found"`；GET 未启动为 `404 / "Workflow not started"`；invalid/repeated start（已有 checkpoint、非 OPEN、缺失/无效 thread binding）为 409；checkpoint/Incident authoritative mismatch 为 409；provider/runtime start failure 为 503。
- [ ] 不得向客户端返回 API key、Authorization header 或 secret configuration value。

## Task 6 — API Tests

- [ ] 在 `test_incidents_api.py` 使用 dependency override 注入 fake `WorkflowRuntime`。
- [ ] POST success：OPEN Incident、POST workflow 返回 200、使用相同 persisted thread ID、Incident 不再 OPEN、返回 WorkflowResponse。
- [ ] unknown Incident：POST 和 GET 均 404；GET before start 为 `404 Workflow not started`。
- [ ] duplicate start：fake runtime 已有 state 时 409，`runtime.start` call count 为 0。
- [ ] non-OPEN start：至少 `WAITING_APPROVAL` 和 `RESOLVED` 都为 409。
- [ ] GET projection：返回 hypotheses/evidence/tool history，Evidence.data 缺失。
- [ ] GET read-only：前后 Incident.updated_at、Report count、Action count、runtime.start calls 不变。
- [ ] runtime failure before checkpoint：POST 为 503，Incident status 恢复 OPEN；wrong checkpoint Incident binding 为 409。

## Task 7 — Local CORS

- [ ] `main.py` 使用 `fastapi.middleware.cors.CORSMiddleware`。
- [ ] 仅允许 `http://localhost:3000` 和 `http://127.0.0.1:3000`；methods 仅 `GET`、`POST`、`OPTIONS`；headers 仅 `Content-Type`。
- [ ] 不使用 wildcard，且不允许 credentials，除非既有 FastAPI 行为明确需要额外 header。
- [ ] 增加最小 OPTIONS/preflight test：localhost allowed、arbitrary origin 不被允许；不引入第三方 CORS package。

## Task 8 — Regression / Acceptance

- [ ] 先运行：

  ```powershell
  cd apps/backend
  python -m pytest tests/test_workflow_console.py -q
  python -m pytest tests/test_incidents_api.py -q
  ```

- [ ] 然后运行：

  ```powershell
  python -m pytest -q
  python -m ruff check .
  alembic upgrade head
  ```

- [ ] 最小人工验证：POST Incident 后，GET workflow 返回 `404 Workflow not started`。
- [ ] 若真实 LLM/Embedding/Fault Lab 可用，验证 start 使用 original thread ID，随后 GET 的 current_stage/hypotheses/evidence/tool_history 来自持久化状态。
- [ ] 若外部 provider 不可用，记录真实 start 为 environment-blocked；自动化测试不得跳过 production runtime composition 或 HTTP orchestration。

## Acceptance Criteria

Task 4.7.0 只有同时满足下列条件才完成：

```text
POST /incidents/{id}/workflow exists
GET  /incidents/{id}/workflow exists
start uses existing thread_id
duplicate start fails 409
non-OPEN start fails 409
GET before start = 404
GET is read-only
response is strict Web projection
raw AgentState is not exposed
Evidence.data is not exposed
authoritative persisted Action is included
Action parameters come from PostgreSQL, not ProposedAction.parameters
production graph is reused
no Agent algorithm changes
local Next.js CORS works
backend full pytest, ruff, and alembic upgrade head pass
```

## Commit

Implementation creates exactly one commit after Tasks 1–7 pass:

```text
feat: add workflow web api
```

Do not commit individual Tasks 1–7: they form one Web API Surface reviewer gate and one plan subtask.

## Plan Self-Review

Before implementation, verify:

```text
无 TODO
无 TBD
无 placeholder
所有接口名称一致
WorkflowResponse 包含 authoritative Action
start 不创建 thread
GET 无副作用
HTTP status 明确
没有 Task 4.7.1 Web UI 工作
没有修改 Agent 核心算法
```
