# DevSupport Agent V1 — Scope Freeze

> Status: Frozen for V1 Execution
>
> This document is the highest-level product and engineering Source of Truth for V1. If it conflicts with V0 documents, this document takes precedence.

---

## 1. V1 Positioning

DevSupport V1 is a Stateful AI Agent that combines engineering knowledge and Runtime Evidence to investigate microservice failures through multiple steps, validate hypotheses, and form grounded conclusions.

Its focus is the AI Investigator, not a complete Incident Management product. V1 builds on the implemented V0 baseline; it does not recreate the existing Incident, RAG, Tool, approval, recovery, Eval, Web, or Fault Lab capabilities.

## 2. V1 Must Prove

V1 execution is frozen around these five outcomes:

1. The Agent can reliably complete multi-step failure investigations, rather than answer a single log question.
2. Hypotheses are driven by both Knowledge and Runtime Evidence.
3. The Workflow has explicit Reliability Budgets, recovery behavior, and terminal boundaries.
4. Underlying data sources can be replaced without rewriting Agent Tool Contracts or the Workflow.
5. Users can understand and demonstrate the investigation process.

## 3. Current Baseline

The following are already implemented and are baseline capabilities to inspect and reuse, not rebuild:

* Incident API and persistence;
* stateful LangGraph workflow with PostgreSQL checkpointing, retry, and resume;
* hypothesis and evidence handling;
* Hybrid RAG (PostgreSQL FTS, pgvector, and RRF) with citation support;
* `query_logs`, `query_metrics`, `query_traces`, and `get_deployment_history` investigation tools;
* policy gate, real human approval, `rollback_deployment`, and recovery verification;
* final report generation;
* Eval fixtures, runner, and automated scoring;
* Web Incident Console;
* the Fault Lab: order-service, payment-service, structured logs, metrics, OpenTelemetry spans, deployment state, fault injection, and reset.

## 4. V1 Scope

### 4.1 Reliability Stabilization

V1 will make the existing workflow observable and bounded through node-level latency, total investigation budget, LLM-call budget, single-call timeout, retry budget, stop conditions, terminal reasons, and a unified failure taxonomy. It should replace unnecessary LLM calls with deterministic logic where the existing evidence or state is sufficient.

The objective is stable Fault Lab regression behavior. Reliability changes must be guided by actual Eval data, not an open-ended cycle of prompt tuning.

### 4.2 Knowledge Grounding

V1 retains the existing Hybrid RAG implementation. It adds only:

* explainable citation and source provenance;
* minimal retrieval regression coverage;
* a small knowledge corpus needed for the real microservice integration.

V1 does not create a Knowledge Management Platform.

### 4.3 Provider-neutral Adapter and Real Integration

The Agent Tool Contract remains unchanged. V1 separates the current concrete Fault Lab adapter dependencies into stable adapter contracts and provider implementations. Conceptually:

```text
query_logs
→ LogsAdapter
→ FaultLabLogsAdapter / RealLogsAdapter
```

Metrics and traces follow the same boundary. Fault Lab remains the Eval environment.

V1 adds a second real open-source microservice environment, with OpenTelemetry Demo preferred. The P0 acceptance is:

* the selected real demo runs;
* DevSupport can query real logs;
* DevSupport can query real metrics;
* at least one end-to-end investigation completes.

Trace integration is P1 / bonus. V1 does not add Kubernetes, database investigation, code investigation, real-demo deployment mutation, or a large provider matrix.

### 4.4 Investigation UX

V1 improves the existing Incident Console instead of redesigning the Web product. Starting an investigation must not keep the browser waiting for the entire workflow before progress is visible. A reasonable implementation may use asynchronous execution with polling, SSE, streaming, or another simple reliable mechanism.

The user-facing view prioritizes an Investigation Timeline plus hypotheses and evidence. Raw Tool arguments and workflow nodes remain available as secondary technical details.

### 4.5 Eval and Release

V1 uses the existing Eval Framework as the release gate rather than rebuilding it. Release evaluation covers root-cause direction, key-evidence recall, tool selection, task completion, timeout, unauthorized execution, latency, LLM calls, and Tool calls.

## 5. Non-goals

V1 does not include:

* Service Registry;
* a full Incident lifecycle redesign;
* an independent Investigation Domain;
* Human Investigation or manual Evidence Management;
* Workspace navigation or Knowledge Management UI;
* Team, tenant, or RBAC capabilities;
* Multi-Agent architecture;
* advanced RAG or OCR;
* Kubernetes remediation, a DB Agent, or a Code Agent;
* a large provider ecosystem;
* a general autonomous remediation platform.

## 6. Fault Lab vs Real Integration

**Fault Lab** is the controlled development, ground-truth, Eval, regression, and fault-injection environment.

**Real Integration** proves that the Agent is not adapted only to a self-built experimental system. Its purpose is to validate adapter generalization. It does not replace Fault Lab as the deterministic Evaluation environment.

## 7. Definition of Done

V1 is done only when all of the following hold:

* core Fault Lab scenarios complete repeatably and stably;
* 120-second timeouts are no longer a widespread normal outcome;
* every terminated investigation has a clear terminal reason;
* conclusions are bound to evidence;
* RAG has regression coverage;
* Tool Runtime is no longer typed to Fault Lab concrete adapter classes;
* the second environment completes at least logs and metrics integration;
* the Web UI shows progress while an investigation runs;
* hypothesis, evidence, timeline, and conclusion are understandable;
* approval, rollback, and recovery verification do not regress;
* the Eval release gate passes;
* Unauthorized Execution equals zero.

## 8. V2 North Star

The future product is an **Incident Investigation Workspace** with business objects:

```text
Service
Incident
Investigation
Evidence
Hypothesis
Knowledge
Report
```

In that product, the Agent is the AI Investigator.

**V2 North Star != V1 Scope.**
