# DevSupport Agent V0 — Pre-Approval Workflow Recovery Design

## 1. Purpose and compatibility

This design extends the Web Console design in
[`2026-08-10-web-console-design.md`](2026-08-10-web-console-design.md). It adds
one narrow recovery path for a persisted, failed investigation before an Action
or Approval exists. It does not change the existing Web Console lifecycle,
workflow start contract, Approval contract, or remediation boundaries.

The recovery path is execution recovery only. It does not make a business
decision and it does not convert an execution failure into an Incident outcome.

## 2. Observed problem

The real Scenario B run established the following durable state:

```text
Incident:    9f3737e2-fd6a-472e-8964-3258dc3c7701
thread:      239e1184-ccbc-4fda-87d9-b6be63678ecf
status:      INVESTIGATING
stage:       investigation_planning
rounds:      2
tool calls:  3
```

The completed investigation work was persisted. The latest failure was:

```text
PlanningError:
planner provider failed:
LLM response did not contain non-empty message content
```

Checkpoint task metadata showed:

```text
previous successful node: planning_guard
next / failed task:       investigation_planning
```

This was a workflow execution/provider failure. It was not an evidence-driven
manual-action conclusion, a safety-limit result, or an Approval rejection.
Consequently, it must not be represented as `NEEDS_MANUAL_ACTION` merely to
make the Incident terminal.

The current start endpoint rejects both an `INVESTIGATING` Incident and any
existing checkpoint. The failed thread therefore has no supported public
continuation path. This is a persisted pre-approval recovery hole.

## 3. Goal

V0 adds an explicit, constrained, same-thread pre-approval retry:

```text
INVESTIGATING + persisted eligible failed task
    -> Retry Investigation
    -> same Incident.thread_id
    -> re-execute the failed LangGraph node
    -> continue the existing production workflow
```

The retry must not replay any successfully checkpointed predecessor.

## 4. Non-goals

This design does not add:

- Automatic or infinite retries.
- A generic workflow replay or time-travel UI.
- Checkpoint editing, a new thread, or restart from Intake.
- Automatic `NEEDS_MANUAL_ACTION` for provider/node failures.
- Approval, Policy, rollback, post-approval remediation, or verification retry.
- Provider fallback, model switching, retry queues, background workers, or
  distributed retry leases.

Day 5 may separately consider LangGraph node `RetryPolicy` and bounded
automatic transient retries.

## 5. Existing start contract remains unchanged

`POST /incidents/{id}/workflow` continues to start only a never-started `OPEN`
Incident. It must continue to return `409` for an existing checkpoint,
`INVESTIGATING`, `WAITING_APPROVAL`, and terminal Incident states. A duplicate
start must never become an implicit resume.

## 6. New endpoint

The recovery operation is a separate endpoint:

```http
POST /incidents/{incident_id}/workflow/retry
```

Its only responsibility is retrying one eligible persisted pre-approval task.
It always uses the exact persisted `Incident.thread_id`; it never creates an
Incident or thread.

## 7. Eligibility and failure provenance

The backend decides eligibility from authoritative PostgreSQL facts and the
latest persisted LangGraph snapshot. It must require all of the following:

- The Incident exists and is `INVESTIGATING`.
- A non-blank stable `thread_id` exists.
- A checkpoint exists for that exact thread.
- The latest snapshot has a failed task with a task error.
- The failed node is an allowlisted pre-approval node.
- No `Action` exists for the Incident.
- No `Approval` exists for the Incident.
- No execution or verification outcome exists in the checkpoint.
- The workflow has not entered `WAITING_APPROVAL` or any later stage.

`INVESTIGATING` alone is insufficient. Failure detection must use LangGraph
snapshot/task metadata, not inferred AgentState fields. A narrow internal
projection may expose only:

```text
retry_available: boolean
failed_node: string | null
safe_error: string | null
```

The public API must not expose a complete `StateSnapshot`, raw exception stack,
or provider payload.

## 8. Retryable node allowlist

The V0 allowlist uses the current production graph node names:

```text
retrieval
hypothesis_generation
investigation_planning
tool_execution
hypothesis_update
evidence_evaluation
resolution_proposal
```

The following nodes are explicitly ineligible:

```text
intake
planning_guard
policy_gate
approval_wait
approval_interrupt
approval_decision
action_execution
recovery_verification
final_report
manual_terminalization
```

This remains a pre-approval investigation/reasoning recovery feature, not a
generic graph resume surface.

## 9. Same-thread recovery semantics

The retry runtime must recompose the same production graph composition used for
initial start: the same RAG service, LLM client, Tool adapters, evaluator,
Policy Gate, Approval services, PostgreSQL checkpointer, terminalization, and
Final Report boundaries.

It must then continue from the existing exact thread checkpoint. The intended
LangGraph semantic is equivalent to:

```python
graph.invoke(None, WorkflowService.config_for(thread_id))
```

The implementation must verify the exact API supported by the installed
LangGraph version before coding. It must not invent checkpoint mutation,
replay logic, or a substitute graph state.

It must not call `create_initial_agent_state(...)`, `WorkflowService.start(...)`,
or create a new Incident/thread.

For the observed Scenario B failure, retry means:

```text
search_knowledge       already completed
query_logs             already completed
query_traces           already completed
planning_guard         already completed
investigation_planning re-executes
```

If planning succeeds, the graph proceeds to its next Tool, hypothesis update,
evidence evaluation, and existing bounded workflow routes. Counters, Evidence,
and Tool History remain those of the checkpoint plus genuinely new work; the
three completed calls are not re-executed.

## 10. Retry result and conflicts

If the retry succeeds, `POST /workflow/retry` returns the normal strict
workflow projection. If the failed provider/node fails again, it returns `503`
with a safe stable detail, keeps the Incident `INVESTIGATING`, retains the same
thread and checkpoint history, and leaves the eligible failed task retryable.

It must not create a second thread, erase evidence, terminalize the Incident,
create an Action/Approval, or execute a side effect.

The endpoint returns:

```text
unknown Incident                    -> 404
no workflow checkpoint              -> 409
healthy workflow / no failed task   -> 409
OPEN Incident                       -> 409
WAITING_APPROVAL                    -> 409
RESOLVED                            -> 409
NEEDS_MANUAL_ACTION                 -> 409
Action already exists               -> 409
post-approval stage                 -> 409
```

## 11. Workflow read projection and Web UX

`GET /incidents/{id}/workflow` gains `retry_available: boolean`, defaulting to
`false`. The backend sets it to `true` only for an eligible persisted failed
checkpoint. The Web must not infer it from status, stage, or elapsed time.

The Incident Console displays **Retry Investigation** only when both of these
authoritative facts hold:

```text
incident.status == INVESTIGATING
workflow.retry_available == true
```

It calls `POST /incidents/{id}/workflow/retry`, disables the button while the
mutation is pending, and refreshes Incident plus Workflow after every result.
On `503`, it displays the safe FastAPI detail and shows Retry again only if the
refreshed projection still reports `retry_available: true`.

The Console must never show Start Investigation and Retry Investigation at the
same time, and it must not create an automatic retry loop.

## 12. Approval retry remains separate

Pre-approval workflow retry is not Approval resume retry. The existing
`POST /incidents/{id}/approval` contract and its same-decision retry after an
Approval `503` remain unchanged.

`/workflow/retry` must never resume an Approval interrupt, submit an Approval
decision, replay Action execution, or enter post-approval paths.

## 13. Concurrency and sequential idempotency

The UI permits one retry mutation at a time by disabling the button. The
backend re-reads the authoritative Incident and latest checkpoint failure
before every retry.

After a retry has successfully advanced the failed node, a second sequential
retry must find no eligible failed task and return `409`; it must not rerun the
successfully advanced node. Strict multi-process retry mutual exclusion needs
additional infrastructure and is out of V0 scope.

## 14. Testing strategy

Runtime coverage must prove:

```text
failed planner checkpoint
-> retry same thread
-> failed planner node re-executes
-> prior successful nodes do not re-run
```

It must also prove that a second provider failure retains checkpoint history and
remains retryable.

API coverage must include eligible retry `200`, retry failure `503`, unknown
Incident `404`, no failed task `409`, `OPEN`/`WAITING_APPROVAL`/terminal states
`409`, existing Action `409`, and duplicate retry after successful advance
`409`.

Projection coverage must prove `retry_available` is true only for an eligible
persisted failed checkpoint.

If the Web has no frontend test framework, implementation verification uses
lint, TypeScript checking, build, code review, and real browser E2E rather than
adding Jest, Vitest, or Playwright solely for this feature.

## 15. Scenario B acceptance

After implementation, establish a new real Scenario B through payment timeout,
Web Incident creation, and Web start. If a natural provider failure occurs,
Retry Investigation must use the same thread and continue the existing
investigation. No artificial provider failure is required when the provider
does not fail naturally; deterministic tests prove the recovery contract.

The final Scenario B outcome remains:

```text
NEEDS_MANUAL_ACTION
no order rollback
no Approval
no Verification PASS
persisted Final Report
```

## 16. Definition of done

A transient eligible pre-approval provider/node failure no longer permanently
strands an Incident. The design preserves one Incident, one thread, the same
persisted investigation, no replay of successful steps, unchanged start
semantics, and all Approval and rollback boundaries.
