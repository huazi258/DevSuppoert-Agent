# DevSupport Agent V1 — Technical Delta

> Status: Frozen for V1 Execution
>
> Related: `docs/V1_SCOPE.md`. This document describes only the V1 changes required relative to the current implementation; it does not replace or duplicate `docs/TECH_DESIGN.md`.

---

## Delta 1 — Reliability Budget

The current workflow already has maximum investigation rounds, maximum Tool calls, a LangGraph recursion limit, checkpointing, retry, and resume. V1 closes the remaining reliability boundary with:

* node-level and LLM-call latency instrumentation;
* a total investigation deadline;
* an LLM-call budget;
* a single-call timeout;
* a retry budget;
* persisted or otherwise observable terminal reasons;
* a unified failure taxonomy shared by API, workflow, Eval, and UI projections.

Optimization is based on measured Eval behavior. Reliability must not be reduced to indefinitely changing prompts.

## Delta 2 — Adapter Contract

Current Tool names are provider-neutral, but `ToolExecutionDependencies` still directly depends on `FaultLabLogsAdapter`, `FaultLabMetricsAdapter`, `FaultLabTracesAdapter`, and `FaultLabDeploymentAdapter`.

V1 changes the execution path to:

```text
Tool
→ Adapter Contract
→ Provider implementation
```

For example:

```text
query_logs
→ LogsAdapter
→ FaultLabLogsAdapter / RealLogsAdapter
```

Provider names must not leak into Agent Workflow or planner Tool naming. Use the smallest stable Python `Protocol`, ABC, or existing-project-style interface that fits the codebase. Do not introduce a large dependency-injection framework.

## Delta 3 — Dual Investigation Environment

Fault Lab remains the deterministic Eval and ground-truth environment. A real integration, preferably OpenTelemetry Demo or an equivalent open-source microservice system, is added to validate provider adapter generalization.

The two environments have different roles and do not replace one another. P0 is real logs and metrics; trace support is bonus scope.

## Delta 4 — Workflow Execution Model

The current start path synchronously waits for the complete `graph.invoke`, which is both a UX issue and a source of request timeout pressure.

V1 makes Start Investigation return promptly while subsequent workflow execution remains persisted. The Web Console can then read running investigation progress. The implementation is intentionally not prescribed: asynchronous execution plus polling, SSE, or another simple reliable method is acceptable based on actual implementation cost.

M4.1 uses an in-process FastAPI background task to decouple HTTP acceptance from execution; it is not a durable distributed job queue, so automatic recovery of a task that has not begun when the application process exits is outside V1 scope.

## Delta 5 — Investigation Event Projection

Internal `node`, `tool`, and `state` events are not the same as a user product Timeline. V1 adds a user-facing Investigation Event projection, for example:

* Investigation started;
* Knowledge searched;
* Logs collected;
* Hypothesis created;
* Hypothesis strengthened or rejected;
* Conclusion reached.

Technical detail may remain available, but it is secondary to the user-facing investigation narrative.

## Delta 6 — Knowledge

Existing Hybrid Retrieval is not redesigned. V1 adds provenance improvements, minimal retrieval regression, and a small corpus for the real integration.

## Delta 7 — Persistence Boundary

Known architectural debt: SQLAlchemy domain `Hypothesis` / `Evidence` and AgentState `HypothesisContext` / `EvidenceContext` are two representations of overlapping concepts.

V1 does not refactor this boundary. It is reserved for a future Workspace / Domain Model effort.

**Do not fix this opportunistically in V1.**
