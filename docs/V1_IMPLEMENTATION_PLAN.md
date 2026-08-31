# DevSupport Agent V1 — Implementation Plan

> Status: Frozen for V1 Execution
>
> Related: `docs/V1_SCOPE.md`, `docs/V1_TECH_DELTA.md`
>
> Estimated total effective development time: 27–40 hours

---

## 1. Execution Rules

V1 is incremental work on the existing V0 implementation, not a Day 1–Day 5 rebuild. Execute exactly one subtask at a time; do not hand an entire milestone to Codex in one task.

Each subtask definition and implementation request must include:

* Goal;
* Existing implementation to inspect;
* Required changes;
* Explicit non-goals;
* Verification;
* Definition of Done.

Before changes, inspect the relevant V1 documents and existing code. Every completed subtask requires its own tested, reviewed Git commit under the workflow defined in `AGENTS.md`.

## 2. Milestones

### M1 — Reliability Stabilization

> Estimate: 6–10 hours. Highest priority.

| Subtask | Goal |
| --- | --- |
| M1.1 | Establish baseline investigation observability: node latency, LLM latency, and existing Eval baseline. |
| M1.2 | Implement a unified investigation budget for total time, LLM calls, Tool calls, rounds, and retries. |
| M1.3 | Define a failure taxonomy and explicit terminal reasons. |
| M1.4 | Add stop conditions and remove unnecessary LLM calls with deterministic logic. |
| M1.5 | Stabilize Fault Lab regression behavior. |

Completion standard: core Fault Lab cases no longer commonly hit 120-second timeouts, and each failure can be located through a clear reason.

### M2 — Knowledge Grounding

> Estimate: 3–5 hours.

| Subtask | Goal |
| --- | --- |
| M2.1 | Audit citation and provenance from retrieval through conclusion. |
| M2.2 | Add minimal RAG regression coverage. |
| M2.3 | Prepare the minimum knowledge corpus for the real integration. |

Explicit non-goal: do not develop a Knowledge Management UI.

### M3 — Provider Decoupling + Real Integration

> Estimate: 8–14 hours.

| Subtask | Goal |
| --- | --- |
| M3.1 | Define minimal adapter contracts. |
| M3.2 | Refactor Fault Lab adapters to implement those contracts. |
| M3.3 | Add adapter contract tests. |
| M3.4 | Bring up OpenTelemetry Demo or the selected equivalent open-source microservice environment. |
| M3.5 | Integrate real logs. |
| M3.6 | Integrate real metrics. |
| M3.7 | Complete one end-to-end real investigation acceptance scenario. |
| M3.8 | Integrate traces as bonus scope. |

The Agent Tool names and Workflow must remain provider-neutral.

### M4 — Investigation UX

> Estimate: 6–10 hours.

| Subtask | Goal |
| --- | --- |
| M4.1 | Decouple Start Investigation from full synchronous workflow execution. |
| M4.2 | Expose retrieval of running investigation progress. |
| M4.3 | Create a user-facing Investigation Timeline projection. |
| M4.4 | Improve hypothesis and evidence presentation. |
| M4.5 | Move technical details into a secondary view. |

Explicit non-goal: do not redesign Workspace information architecture.

### M5 — Eval Calibration + Release

> Estimate: 4–7 hours.

| Subtask | Goal |
| --- | --- |
| M5.1 | Select the P0 Fault Lab regression suite. |
| M5.2 | Tune Eval thresholds and reporting without weakening ground truth. |
| M5.3 | Add real-integration acceptance Eval. |
| M5.4 | Run end-to-end recovery and approval regression. |
| M5.5 | Verify the V1 Release Gate. |
| M5.6 | Update README only after implementation truth is final. |

## 3. V1 Release Gate

Release requires the Definition of Done in `docs/V1_SCOPE.md` and evidence that:

* Fault Lab P0 regression is stable within the defined budgets;
* terminal reasons and failure taxonomy are visible in Eval reporting;
* citation provenance and retrieval regression pass;
* the real integration completes logs, metrics, and one E2E investigation;
* running investigation progress and user-facing Timeline are usable in the existing Console;
* approval, rollback, recovery verification, and Unauthorized Execution safeguards remain correct.
* the separate real-integration acceptance artifact satisfies its evaluator-only policy; it never weakens Fault Lab P0 ground truth or substitutes for the Fault Lab release gate.

## 4. Sequencing Constraints

M1 is completed before tuning the remaining milestones. M2 and M3 may proceed only after the relevant reliability baseline exists. M4 is an incremental Console improvement, not a Workspace redesign. M5 validates implementation truth and must not weaken Fault Lab ground truth to make a release pass.

Complete the current subtask, commit and push it, then stop. Do not automatically begin the next subtask.
