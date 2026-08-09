# Web E2E & Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在真实 PostgreSQL、Fault Lab、RAG、LLM/Embedding provider、FastAPI 与 Next.js 环境下，从浏览器验证完整的 Incident investigation、approval、remediation、verification 与 reporting 链路；代码修改只允许修复真实 E2E 暴露的演示阻塞问题。

**Architecture:** 不新增产品架构。浏览器始终走 `Next.js → FastAPI → PostgreSQL + LangGraph → Fault Lab / LLM / RAG`。Task 4.7.2 以真实运行验证为主，任何产品代码改动必须由该验证直接驱动。

**Tech Stack:** Existing Next.js 15 / React 19 / FastAPI / PostgreSQL / pgvector / LangGraph / local Fault Lab / configured OpenAI-compatible providers.

## Global Constraints

* 不使用 mock LLM、fake Embedding 或测试 workflow 替代 production workflow；不跳过 RAG。
* 不 hardcode Scenario A/B root cause，不直接修改 AgentState，也不人工插入 Approval、Action、Verification 或 Report 来伪造结果。
* 不直接调用 rollback 来替代 Web Approval 路径；Web 不直接访问 Fault Lab。
* Tool success 不等于 Incident resolved；Recovery Verification 必须走既有正式 workflow。
* 只允许最小 E2E-driven polish，不做视觉重构，不新增业务功能、浏览器测试框架、SSE/WebSocket、UI framework 或 backend business rule。
* provider 环境缺失时，E2E 必须标记 `NOT ACCEPTED / ENVIRONMENT BLOCKED`，不能声称完成或创建完成 commit。

## Task 1 — Environment Preflight

- [ ] From repository root, start and verify the sole compose dependency:

  ```powershell
  docker compose up -d postgres
  docker compose ps
  ```

  PostgreSQL must be healthy before proceeding.

- [ ] From `apps/backend`, run `alembic upgrade head`.
- [ ] Check provider configuration without printing values. The required workflow inputs are `LLM_MODEL`, `LLM_API_KEY`, `EMBEDDING_MODEL`, and `EMBEDDING_API_KEY`; `LLM_BASE_URL` and `EMBEDDING_BASE_URL` may use configured defaults or their `DEVSUPPORT_` aliases. Use a boolean-only check:

  ```powershell
  @'
  from devsupport_backend.config import settings
  print({
      "llm_model": bool(settings.llm_model),
      "llm_api_key": settings.llm_api_key is not None,
      "embedding_model": bool(settings.embedding_model),
      "embedding_api_key": settings.embedding_api_key is not None,
  })
  '@ | python -
  ```

- [ ] If a required value is missing, record exact missing variable names only, stop E2E execution, and do not create an E2E completion commit.
- [ ] Check corpus counts through the existing ORM boundary; do not create rows manually:

  ```powershell
  @'
  from sqlalchemy import func, select
  from devsupport_backend.database import SessionLocal
  from devsupport_backend.models import KnowledgeChunk, KnowledgeDocument
  with SessionLocal() as session:
      print({
          "documents": session.scalar(select(func.count()).select_from(KnowledgeDocument)),
          "chunks": session.scalar(select(func.count()).select_from(KnowledgeChunk)),
      })
  '@ | python -
  ```

- [ ] If either count is zero, run the actual ingestion entry point from `apps/backend` using the configured real embedding provider:

  ```powershell
  python -m devsupport_backend.rag.ingest
  ```

  The command uses the repository `knowledge` directory by default and is the only approved corpus initialization path.

## Task 2 — Start the Real Local Stack

- [ ] In separate terminals, install/editable-run the services and verify their health:

  ```powershell
  cd services/payment-service
  python -m pip install -e .
  uvicorn payment_service.main:app --host 127.0.0.1 --port 8001
  ```

  ```powershell
  cd services/order-service
  python -m pip install -e .
  uvicorn order_service.main:app --host 127.0.0.1 --port 8000
  ```

  `GET http://127.0.0.1:8001/health` and `GET http://127.0.0.1:8000/health` must return 200. The order service default payment URL is `http://127.0.0.1:8001`.

- [ ] Start FastAPI from `apps/backend` and verify `GET http://127.0.0.1:8002/health` returns 200:

  ```powershell
  uvicorn devsupport_backend.main:app --host 127.0.0.1 --port 8002
  ```

- [ ] Start Web from `apps/web` with the configured local API URL and verify `http://localhost:3000` loads:

  ```powershell
  $env:NEXT_PUBLIC_DEVSUPPORT_API_BASE_URL="http://127.0.0.1:8002"
  npm run dev
  ```

## Task 3 — Fault Lab Baseline

- [ ] Before each independent scenario, reset both live services only at scenario start:

  ```powershell
  Invoke-RestMethod -Method Post http://127.0.0.1:8000/internal/fault-lab/reset
  Invoke-RestMethod -Method Post http://127.0.0.1:8001/internal/fault-lab/reset
  ```

- [ ] Confirm order deployment is `v1.0.0`, `POST /orders` succeeds with 200, and payment has no injected delay. Do not reset during an Incident investigation; rollback must preserve its logs, traces, and metrics.

## Task 4 — Scenario A Approve Path

- [ ] Activate the real Scenario A state from `services/order-service` using its supported control command:

  ```powershell
  python -m order_service.fault_control inject missing_config
  ```

- [ ] Verify `GET /internal/deployment` reports `current_version: v1.1.0` and `previous_version: v1.0.0`. Submit a real `POST /orders` request and capture the request time window; it must return 500.
- [ ] Read Fault Lab observability to confirm `MissingRequiredConfiguration`, error-count growth, and a failed trace exist. The Incident description remains user-observable only, e.g. `order-service requests started returning HTTP 500 after a recent deployment.`; it must not include root cause or rollback instructions.
- [ ] In the browser create the Incident with a time range covering the observed request, record Incident ID and persisted thread ID, then press Start Investigation.
- [ ] Observe real state/progress: Incident status begins at `INVESTIGATING`; Agent stage changes; hypotheses, Evidence, and Tool Timeline appear. Confirm the actual production investigation includes RAG and the necessary real deployment/logs/metrics/traces evidence selected by the Planner, without requiring a fixed tool sequence.
- [ ] Wait for `WAITING_APPROVAL` and `waiting_approval`. Verify the displayed persisted authoritative Action is `order-service`, `local`, `v1.1.0 → v1.0.0` and is sourced from `workflow.action.parameters`.
- [ ] Click Approve only in the Web UI. Observe `REMEDIATING → VERIFYING → RESOLVED`, then confirm Approval `APPROVED`, Action `EXECUTED`, Verification `PASS`, and persisted Final Report visible.
- [ ] Confirm deployment changes `v1.1.0 → v1.0.0`, a new `POST /orders` succeeds with 200, and pre-rollback logs/traces/metrics remain available.

## Task 5 — Scenario A Audit Cross-check

- [ ] Use read-only API and PostgreSQL queries for the approved Incident. Confirm exactly one stable thread, Action, Approval, Verification, and Report.
- [ ] Confirm `Approval.action_id == Action.id`, `Verification.action_id == Action.id`, and `Report.incident_id == Incident.id`; compare `GET /incidents/{id}`, `/workflow`, and `/report` against the browser display.
- [ ] Retry `POST /incidents/{id}/workflow`; it must return 409 and create no second investigation.

## Task 6 — Scenario A Reject Path

- [ ] Reset, reinject Scenario A, and generate a fresh real 500 request.
- [ ] Through the browser create a new Incident, Start the real investigation, and wait for `WAITING_APPROVAL` with an authoritative Action.
- [ ] Click Reject. Confirm Approval `REJECTED`, Incident `NEEDS_MANUAL_ACTION`, deployment remains `v1.1.0`, no rollback occurs, no Verification `PASS` is fabricated, and a Final Report is visible with final status `NEEDS_MANUAL_ACTION`.

## Task 7 — Scenario B Manual-action Path

- [ ] Reset Fault Lab and verify order-service remains at healthy baseline. From `services/payment-service`, inject the supported real payment delay:

  ```powershell
  python -m payment_service.fault_control inject payment_timeout
  ```

- [ ] Generate a real slow/502 order request. Confirm payment latency, order timeout, an order-to-payment trace, and no new order deployment.
- [ ] Through the browser create an Incident with an observable-only description such as `order-service requests are slow and failing while calling payment-service.`, then Start Investigation.
- [ ] Confirm the established Scenario B manual terminal conclusion (`NEEDS_MANUAL_ACTION` or its accepted equivalent) has no order rollback Action/Approval, no Recovery Verification `PASS`, and a persisted Final Report. An incorrect order rollback is an E2E failure: stop and report it without modifying UI to hide it.

## Task 8 — Approval 503 Retry UX

- [ ] At a real `WAITING_APPROVAL`, create an Approval resume failure by temporarily stopping a necessary local continuation dependency without changing production business rules, records, checkpoints, or browser API behavior.
- [ ] Click Approve in the browser and confirm 503 detail is visible, ordinary Approve/Reject controls disappear, and only Retry Approve appears. Repeat analogously for Reject if an isolated safe run is available.
- [ ] Restore the dependency and use the matching retry only. Confirm continuation resumes without a second Approval record.
- [ ] After every refresh, verify stale retry is absent once workflow has moved beyond `waiting_approval` or a real approval outcome exists. If this fails, apply only the smallest Web-side state-clearing polish; do not modify backend semantics.

## Task 9 — Polling, Terminal, and Layout Validation

- [ ] During investigation, use browser network inspection or an equivalent read-only observation to confirm Incident and Workflow reads occur around every 2.5 seconds without an overlapping-request storm.
- [ ] Confirm terminal `RESOLVED` / `NEEDS_MANUAL_ACTION` stops workflow polling, Final Report comes only from the persisted report endpoint, and reloading a terminal route restores persisted state/report.
- [ ] Polish only actual E2E blockers: invalid controls on uncertain state, misleading status/stage, unreadable crucial report facts, UUID/JSON overflow, invisible API errors, stale Approval retry, or repeated polling. Do not add pages, features, charts, animation, or visual redesign.

## Task 10 — Regression, Evidence, and Commit

- [ ] If Web changes, run from `apps/web`:

  ```powershell
  npm run lint
  npx tsc --noEmit
  npm run build
  ```

- [ ] Run `python -m pytest -q` from `apps/backend`. Do not claim full Ruff clean for the documented historical migration debt; if no Python changes occur, do not change migrations merely for lint.
- [ ] Run `git diff --check`, `git diff --stat`, and `git status --short`; do not commit secrets, provider payloads, or large raw logs.
- [ ] Completion Report must give three real Incident IDs (Scenario A Approve, Scenario A Reject, Scenario B Manual) and for each: thread ID, final Incident status/current stage, Action/Approval/Verification ID/status where present, Report ID, and final deployment state. Scenario A Approve additionally records `v1.1.0 → v1.0.0` and successful recovery probe.
- [ ] Do not create an empty Task 4.7.2 commit. If a true E2E-driven Web fix is needed, commit `fix: polish web incident e2e`; if all E2E passes with no code changes, document the actually used successful runbook and commit `docs: document web e2e workflow`. Push `origin/main` and stop.

## Acceptance Criteria

- [ ] Real browser Scenario A completes Create → Start → RAG/Tool investigation → WAITING_APPROVAL → authoritative Action → Web Approve → real rollback → Verification PASS → RESOLVED → persisted Final Report.
- [ ] Real browser Reject completes WAITING_APPROVAL → Reject → NEEDS_MANUAL_ACTION without rollback.
- [ ] Real browser Scenario B reaches manual terminal state without incorrect order rollback or Verification PASS.
- [ ] One Incident has one thread; duplicate start is 409; terminal polling stops; Approval 503 retry is safe.
- [ ] Web never accesses Fault Lab, executes rollback, changes Action parameters, or fabricates status.
- [ ] Web lint/type/build and backend full pytest pass.

## Plan Self-Review

- [ ] Real supported Fault Lab control commands are used for Scenario A and B.
- [ ] Browser Approve, Reject, Manual, 503 retry, terminal polling, and audit checks are explicit.
- [ ] No mock production workflow, fake Verification, direct rollback, hardcoded root cause, or backend rule change is planned.
- [ ] Provider absence is an environment block, never a completed E2E claim.
