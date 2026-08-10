# Pre-Approval Workflow Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

Implement a narrow recovery path for a persisted failed pre-approval workflow:

```text
persisted failed pre-approval task
-> backend identifies exact failed task
-> GET workflow exposes retry_available
-> POST /workflow/retry
-> same thread
-> re-execute only the failed node
-> continue the existing production graph
```

This is durable execution recovery. It must not change Agent reasoning,
investigation safety limits, Planner prompts, Policy, Approval, rollback,
verification, or Final Report semantics.

## Verified LangGraph continuation contract

The installed package is `langgraph==0.6.11`. An isolated `StateGraph` with an
`InMemorySaver` was exercised against a deliberately failing node:

```text
graph.invoke(initial_state, same_config) -> RuntimeError
graph.get_state(same_config).tasks       -> failed task name and task.error
graph.get_state(same_config).next        -> failed node
graph.invoke(None, same_config)          -> reruns the failed node only
```

The successful retry completed with the original thread config and did not
rerun prior successful nodes. Therefore implementation uses the verified
same-thread continuation API:

```python
graph.invoke(None, WorkflowService.config_for(thread_id))
```

It must not mutate checkpoint rows, create an AgentState, call
`create_initial_agent_state(...)`, call `WorkflowService.start(...)`, or
create a new Incident/thread.

## Current implementation anchors

- `apps/backend/src/devsupport_backend/agent/runtime.py` owns the generic
  `WorkflowService` and the sole `config_for(thread_id)` shape.
- `apps/backend/src/devsupport_backend/workflow_console.py` owns the formal
  PostgreSQL runtime composition and `WorkflowConsoleService` lifecycle
  boundary.
- `apps/backend/src/devsupport_backend/routers/incidents.py` maps console
  service errors to HTTP responses.
- `apps/backend/src/devsupport_backend/schemas/workflows.py` defines the strict
  public workflow response.
- `apps/web/src/components/incident-console.tsx` already fail-closes mutation
  controls when workflow state is loading or invalid.

The current graph names are:

```text
retryable: retrieval, hypothesis_generation, investigation_planning,
           tool_execution, hypothesis_update, evidence_evaluation,
           resolution_proposal

ineligible: intake, planning_guard, policy_gate, approval_wait,
            approval_interrupt, approval_decision, action_execution,
            recovery_verification, final_report, manual_terminalization
```

## Task 1 — Runtime failed-task inspection and same-thread continuation

Files:

- Modify `apps/backend/src/devsupport_backend/agent/runtime.py`
- Add `apps/backend/tests/test_pre_approval_workflow_recovery.py`

- [ ] Write `test_workflow_service_inspects_allowlisted_failed_task_before_retry`.
  Build a deterministic checkpointer-backed graph where a successful
  predecessor runs once and `investigation_planning` fails. Assert the initial
  RED failure because the runtime has no failure-inspection contract. The test
  must inspect the persisted snapshot through the runtime and assert the exact
  node name plus a safe error string, without exposing a raw traceback.

- [ ] Write `test_workflow_service_retries_failed_planner_on_same_thread_without_replaying_predecessors`.
  Persist one successful predecessor and a one-time planner failure, then retry
  using the same `thread_id`. Initially this is RED because no failed-task
  continuation exists. The passing assertions are: predecessor count remains
  one, planner count becomes two, the continuation has no new thread, and prior
  state/counters/history remain available to the retried node.

- [ ] Write `test_workflow_service_second_failed_retry_remains_inspectable`.
  Make the retryable planner fail twice. It is initially RED because no retry
  method exists. On pass, the second failure leaves the same thread's latest
  snapshot with `next == ["investigation_planning"]`, a task error, and an
  inspectable eligible failure for a later explicit retry.

- [ ] Implement a small typed runtime contract in `agent/runtime.py`, for
  example a frozen `WorkflowFailure` containing `failed_node` and `safe_error`.
  Add `WorkflowService.get_failure(thread_id)` that reads the current
  `StateSnapshot.tasks` and returns a failure only when exactly one failed task
  belongs to the pre-approval allowlist. Add
  `WorkflowService.retry_failed_task(thread_id)` that uses the verified
  `graph.invoke(None, config_for(thread_id))` continuation.

- [ ] Keep state inspection separate from continuation. `get_state()` remains
  the existing AgentState-value reader and Approval's existing
  `Command(resume=...)` contract is unchanged.

- [ ] Run RED tests first, implement the minimum runtime boundary, then run:

  ```powershell
  cd apps/backend
  python -m pytest tests/test_pre_approval_workflow_recovery.py -q
  python -m pytest tests/test_persistent_workflow.py tests/test_workflow_resume.py -q
  python -m ruff check src/devsupport_backend/agent/runtime.py tests/test_pre_approval_workflow_recovery.py
  ```

- [ ] Review `git diff --check`, `git diff --stat`, and `git status --short`.
  Commit only this independently testable runtime foundation, push `origin/main`,
  and stop for review before Task 2.

## Task 2 — Backend eligibility service and strict read projection

Files:

- Modify `apps/backend/src/devsupport_backend/workflow_console.py`
- Modify `apps/backend/src/devsupport_backend/schemas/workflows.py`
- Modify `apps/backend/tests/test_workflow_console.py`

- [ ] Extend the existing `WorkflowRuntime` protocol and
  `PostgresWorkflowRuntime` to expose the Task 1 failure inspection and retry
  capabilities while preserving the existing production graph composition for
  start and retry.

- [ ] Write `test_retry_available_requires_exact_failed_preapproval_task`.
  Start RED with a fake runtime failure state and assert `WorkflowResponse` has
  no retry field. Once implemented, assert `retry_available` is true only for
  a failed allowlisted pre-approval task on an `INVESTIGATING` Incident.

- [ ] Write `test_retry_available_rejects_ineligible_node_and_non_investigating_status`.
  Assert false for `policy_gate`, `approval_wait`, and `final_report` failures,
  and for `OPEN`, `WAITING_APPROVAL`, `RESOLVED`, and
  `NEEDS_MANUAL_ACTION` Incident states.

- [ ] Write `test_retry_available_rejects_persisted_action_approval_and_postapproval_outcomes`.
  Build each authoritative conflict using the existing `Action` and `Approval`
  models plus checkpoint outcomes. Assert false when an Action exists, an
  Approval exists, an execution outcome exists, or a verification outcome
  exists; do not substitute `Action is None` for these independent checks.

- [ ] Implement one `WorkflowConsoleService` eligibility helper that combines
  authoritative Incident status/thread facts, runtime failure provenance,
  PostgreSQL Action/Approval facts, and checkpoint execution/verification
  outcomes. The helper is the only source for the projection and future retry
  mutation eligibility.

- [ ] Add `retry_available: bool = False` to `WorkflowResponse` in
  `schemas/workflows.py`, and populate it from the eligibility helper in
  `project_workflow_response`. Do not add a public raw error, failed-node,
  StateSnapshot, provider response, or stack trace field.

- [ ] Preserve all existing incident/checkpoint binding validation and invalid
  persisted Action-parameter conflict behavior.

- [ ] Run RED tests first, implement, then run:

  ```powershell
  cd apps/backend
  python -m pytest tests/test_workflow_console.py -q
  python -m pytest tests/test_investigation_workflow.py -q
  python -m ruff check src/devsupport_backend/workflow_console.py src/devsupport_backend/schemas/workflows.py tests/test_workflow_console.py
  ```

- [ ] Check diff/status, commit this projection boundary, push `origin/main`,
  and stop for review before Task 3.

## Task 3 — Explicit retry API with fail-closed conflicts

Files:

- Modify `apps/backend/src/devsupport_backend/workflow_console.py`
- Modify `apps/backend/src/devsupport_backend/routers/incidents.py`
- Modify `apps/backend/tests/test_incidents_api.py`
- Modify `apps/backend/tests/test_workflow_console.py` only if a service-level
  retry method requires direct coverage beyond Task 2.

- [ ] Write `test_workflow_retry_api_retries_eligible_failure_on_the_original_thread`.
  The initial RED failure is `404` because the route does not exist. Use the
  existing dependency override pattern and a deterministic fake runtime. On
  pass, assert `200`, one retry invocation with the Incident's persisted
  `thread_id`, normal `WorkflowResponse`, and no new start invocation.

- [ ] Write `test_workflow_retry_api_returns_service_unavailable_and_keeps_retryable_state`.
  Make runtime retry raise after a checkpointed failure. Initially RED because
  the route is absent. On pass, assert `503` with a safe detail, Incident still
  `INVESTIGATING`, same thread, no Action/Approval, and a subsequent GET
  projection retaining `retry_available: true`.

- [ ] Write parameterized
  `test_workflow_retry_api_rejects_ineligible_lifecycle_and_persistence_states`.
  Cover unknown Incident (`404`); `OPEN`, no checkpoint, no failed task,
  ineligible failure node, `WAITING_APPROVAL`, `RESOLVED`,
  `NEEDS_MANUAL_ACTION`, persisted Action, and post-approval outcomes (`409`).
  Each begins RED because the endpoint does not exist.

- [ ] Write `test_workflow_retry_api_rejects_sequential_duplicate_after_advance`.
  After the first fake retry clears or advances the failed task, the second
  request must be `409` and must not increment the retry count.

- [ ] Keep retry orchestration in `WorkflowConsoleService.retry(incident_id)`.
  The router only maps lookup to `404`, eligibility/conflict failures to `409`,
  and retry execution failure to `503`. Re-read authoritative eligibility
  immediately before invoking runtime retry.

- [ ] Do not alter `WorkflowConsoleService.start` or
  `POST /incidents/{id}/workflow`; retain the existing duplicate-start tests and
  add an explicit regression assertion that `INVESTIGATING` plus a checkpoint
  remains `409` on start.

- [ ] Run RED tests first, implement, then run:

  ```powershell
  cd apps/backend
  python -m pytest tests/test_incidents_api.py -q
  python -m pytest tests/test_workflow_console.py tests/test_pre_approval_workflow_recovery.py -q
  python -m pytest tests/test_human_approval.py tests/test_workflow_resume.py -q
  python -m ruff check src/devsupport_backend/workflow_console.py src/devsupport_backend/routers/incidents.py tests/test_incidents_api.py
  ```

- [ ] Check diff/status, commit the API boundary, push `origin/main`, and stop
  for review before Task 4.

## Task 4 — Web Retry Investigation UX

Files:

- Modify `apps/web/src/lib/types.ts`
- Modify `apps/web/src/lib/api.ts`
- Modify `apps/web/src/components/incident-console.tsx`

- [ ] Update `WorkflowResponse` with `retry_available: boolean` and add a
  `retryWorkflow(id)` API wrapper for `POST /incidents/{id}/workflow/retry`.
  TypeScript must fail before this update where the Console uses the new field.

- [ ] Add `retryInvestigation()` to the existing `IncidentConsole` mutation
  boundary. It clears mutation error, calls the new API wrapper, and refreshes
  authoritative Incident and Workflow after both success and error.

- [ ] Add a `canRetryInvestigation` gate that requires:

  ```text
  incident.status === "INVESTIGATING"
  workflow?.retry_available === true
  !workflowLoading
  workflowError === null
  !mutationPending
  ```

  Render **Retry Investigation** only through this gate. The RED static/type
  expectation is that no retry control exists until the response/API changes
  are made; the passing implementation cannot display it beside Start
  Investigation or infer it from elapsed time/status.

- [ ] During the mutation disable the sole Retry control. On `503`, render the
  safe FastAPI detail and let refreshed `retry_available` alone decide whether
  it reappears. Do not create a retry timer, state library, frontend test
  framework, or Approval retry coupling.

- [ ] Run:

  ```powershell
  cd apps/web
  npm run lint
  npx tsc --noEmit
  npm run build
  ```

- [ ] Review the control gates manually for unknown workflow state,
  `workflowLoading`, and `workflowError`. Check diff/status, commit this Web
  boundary, push `origin/main`, and stop for review before Task 5.

## Task 5 — Deterministic recovery acceptance and Scenario B E2E

Files:

- No planned production-file change.
- Add or adjust only focused tests required by an E2E-discovered product defect;
  such a defect receives its own review-fix commit.

- [ ] Run the deterministic recovery tests from Tasks 1–3 first. They must
  prove failed planner checkpoint detection, retry availability, same-thread
  continuation, no predecessor replay, repeated-failure retryability, `503`,
  conflicts, duplicate retry `409`, and unchanged duplicate-start `409`.

- [ ] If still complete and eligible after implementation, use the existing
  Scenario B Incident `9f3737e2-fd6a-472e-8964-3258dc3c7701` only through the
  Web Console: reload, confirm Retry Investigation, click it, and verify the
  same thread continues. Do not depend on this artifact if it was cleaned or is
  no longer eligible.

- [ ] Otherwise create a new real payment-timeout Scenario B through the Web.
  Do not manufacture provider failures. If a natural pre-approval provider
  failure occurs, use Web Retry Investigation and verify same-thread
  continuation. If it does not occur, deterministic tests remain the recovery
  contract evidence.

- [ ] Confirm the terminal Scenario B remains `NEEDS_MANUAL_ACTION`, with no
  order-service rollback, no Approval, no Verification `PASS`, a persisted
  Final Report, and order deployment at its healthy baseline.

- [ ] Run final regression:

  ```powershell
  cd apps/backend
  python -m pytest -q
  python -m ruff check src/devsupport_backend/agent/runtime.py src/devsupport_backend/workflow_console.py src/devsupport_backend/routers/incidents.py

  cd ../web
  npm run lint
  npx tsc --noEmit
  npm run build

  cd ../..
  git diff --check
  git diff --stat
  git status --short
  ```

## Global safety invariants

- No Agent reasoning, hypothesis, evaluator, Planner prompt, Tool selection, or
  investigation limit change.
- No Policy, Approval, rollback, recovery verification, or Final Report
  semantic change.
- No new thread, checkpoint SQL edit, manual AgentState injection, Intake
  replay, automatic manual terminalization, general replay endpoint, or
  post-approval retry.
- Existing Approval `503` behavior remains separate: persist decision first,
  resume the same approval thread for the same decision only, and reject the
  opposite decision.
- Existing start behavior remains `OPEN + no checkpoint` only.

## Commit sequence

Each of Tasks 1–4 is one independently reviewable implementation commit. Run
its focused verification, `git diff --check`, `git diff --stat`, and
`git status --short`; push `origin/main`; then stop for review. Do not amend,
force-push, or combine unrelated E2E fixes with these commits.

## Plan self-review

Every design requirement maps to a task. Paths and current APIs above were
verified against the repository. The plan uses the tested LangGraph `0.6.11`
continuation API, contains no checkpoint mutation, no hidden generic replay,
no post-approval expansion, no automatic retry, and no weakened Start or
Approval boundary.
