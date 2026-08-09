# Web Console UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用现有 Next.js 15 / React 19 Web shell 构建 DevSupport Agent V0 可操作 Console，使用户可以创建 Incident、启动调查、观察 Agent 状态、审批 Action、查看 Recovery Verification 与 Final Report。

**Architecture:** Web 只通过 FastAPI REST 访问后端。建立集中 typed API client 和 TypeScript contracts；首页负责 Incident List/Create，`/incidents/[id]` 使用单个 client-side Incident Console 管理 polling 与 mutations；展示组件保持纯 presentational。Web 不拥有 Agent 业务规则，也不自行推导 remediation 权限。

**Tech Stack:** Next.js 15.5、React 19、TypeScript strict mode、browser fetch、native CSS。不得引入 UI framework、state-management library、SSE/WebSocket 或新的测试框架。

## Global Constraints

* 不修改 backend Agent 算法，也不直接访问 PostgreSQL、Fault Lab、LLM 或 Tool。
* Web 不执行 rollback；Approval 只提交 `APPROVE` / `REJECT`，且不允许 Web 修改任何 Action parameters。
* Incident Status 与 Agent Stage 必须分别展示。
* polling 只使用 REST，约 2500ms；terminal Incident 停止 workflow polling。
* Final Report 只展示 persisted report。
* Task 4.7.1 不修改 backend Python；Task 4.7.2 的真实浏览器 E2E 不在本轮完成。

## File Structure

- [ ] Create `apps/web/src/lib/types.ts`: 公开 FastAPI DTO 的严格 TypeScript contracts。
- [ ] Create `apps/web/src/lib/api.ts`: 唯一 browser REST client 与错误边界。
- [ ] Create `apps/web/src/components/status-badge.tsx`。
- [ ] Create `apps/web/src/components/incident-create-form.tsx`。
- [ ] Create `apps/web/src/components/incident-list.tsx`。
- [ ] Create `apps/web/src/components/incident-console.tsx`。
- [ ] Create `apps/web/src/components/workflow-view.tsx`。
- [ ] Create `apps/web/src/components/final-report.tsx`。
- [ ] Create `apps/web/src/app/incidents/[id]/page.tsx`。
- [ ] Modify `apps/web/src/app/page.tsx`, `layout.tsx`, `globals.css`, and `package.json` only.

Do not create Redux/Zustand stores, server actions, API proxy routes, WebSocket/SSE, authentication, dashboards, chart libraries, or component libraries.

## Task 1 — TypeScript API Contracts

- [ ] In `src/lib/types.ts`, define `IncidentStatus` as the known UI statuses (`OPEN`, `INVESTIGATING`, `WAITING_APPROVAL`, `REMEDIATING`, `VERIFYING`, `RESOLVED`, `NEEDS_MANUAL_ACTION`), but keep `Incident.status: string` so the UI safely renders future backend statuses.
- [ ] Define `Incident` with `id`, `service`, `environment`, `description`, `status`, `time_range_start`, `time_range_end`, `thread_id`, `created_at`, and `updated_at`; define `CreateIncidentInput` with the create fields only.
- [ ] Define `WorkflowHypothesis`, `WorkflowEvidence`, `WorkflowToolError`, `WorkflowToolHistory`, `WorkflowFinalConclusion`, `WorkflowProposedAction`, `WorkflowPolicy`, `WorkflowActionParameters`, `WorkflowAction`, `WorkflowApprovalOutcome`, `WorkflowExecutionOutcome`, `WorkflowVerificationOutcome`, `WorkflowReportOutcome`, and `WorkflowResponse` to exactly match the 4.7.0 response models.
- [ ] `WorkflowEvidence` includes only `id`, `evidence_type`, `source`, `summary`, and `reference`; it must not contain `data`.
- [ ] `WorkflowProposedAction` includes `action_type`, `summary`, `reason`, `risk`, and supporting Evidence IDs; it must not contain `parameters`.
- [ ] `WorkflowResponse` contains incident IDs/status/stage; hypotheses, evidence, and tool history; current goal; final conclusion; proposed action; policy; authoritative action; approval, execution, verification, and report outcomes.
- [ ] Define `ApprovalDecision = "APPROVE" | "REJECT"`.
- [ ] Define `FinalReport` as the persisted report envelope and `FinalReportContent` exactly matching backend `final_report.FinalReportContent`: `schema_version`, incident summary, optional root cause, hypotheses, key evidence, optional recommended action/action/approval/execution/verification, ordered timeline, and final status. Define corresponding named section types: `FinalReportIncidentSummary`, `FinalReportRootCause`, `FinalReportHypothesis`, `FinalReportEvidence`, `FinalReportRecommendedAction`, `FinalReportAction`, `FinalReportApproval`, `FinalReportExecution`, `FinalReportVerification`, and `FinalReportTimelineItem`.
- [ ] Do not use TypeScript `any`; use `Record<string, unknown>` for arbitrary JSON objects such as persisted Action parameters and verification details.

## Task 2 — Central API Client

- [ ] In `src/lib/api.ts`, define the sole backend URL:

  ```ts
  const API_BASE_URL =
    process.env.NEXT_PUBLIC_DEVSUPPORT_API_BASE_URL ?? "http://127.0.0.1:8002";
  ```

- [ ] Implement `ApiError` with `status` and safe backend `detail`, and one generic `request<T>(path, init?)` helper.
- [ ] The helper sends `Accept: application/json`, sends `Content-Type: application/json` only with a body, and uses `cache: "no-store"`. It reads FastAPI `{ detail }` errors before throwing `ApiError`; it never silently swallows failures.
- [ ] Export `listIncidents`, `createIncident`, `getIncident`, `startWorkflow`, `getWorkflow`, `submitApproval`, and `getFinalReport`.
- [ ] `submitApproval(id, decision)` sends exactly `{ decision }`. Never send action ID, service, environment, target version, or parameters.
- [ ] Components must not hardcode a backend URL. Do not update the README port guidance during this task.

## Task 3 — Status Badge

- [ ] Implement pure `StatusBadge({ value: string })` in `status-badge.tsx`.
- [ ] Render the original value even when unknown. CSS may group known Incident statuses, hypothesis states (`ACTIVE`, `SUPPORTED`, `REJECTED`, `CONFIRMED`), and verification states (`PASS`, `FAIL`, `INCONCLUSIVE`), but color must not determine business logic.

## Task 4 — Incident Creation

- [ ] Implement client `IncidentCreateForm` with `service`, `environment`, `time_range_start`, `time_range_end`, and `description`.
- [ ] Provide V0 select options `order-service` / `payment-service` and `local` / `production`; production is intentionally available to demonstrate backend Policy denial.
- [ ] Use `datetime-local` inputs. Initialize start to local browser time minus 15 minutes and end to local browser time; use a local-time formatting helper rather than slicing `toISOString()`.
- [ ] Validate non-empty fields, trimmed description, and end not before start. Submit timezone-aware values from `new Date(value).toISOString()`.
- [ ] Disable submit while pending, show a visible API error banner, and on success route to `/incidents/${incident.id}`. Do not reproduce backend Policy validation.

## Task 5 — Incident List and Home

- [ ] Implement client `IncidentList`: request `listIncidents()` on mount, render Service, Environment, Status, and Created columns/rows linking to `/incidents/{id}`, and offer explicit Refresh.
- [ ] Preserve prior list data when refresh fails and show the backend error visibly. Render `No incidents yet.` for a successful empty response. Do not add search, pagination, filters, or bulk actions.
- [ ] Update `app/page.tsx` to compose a concise header, `IncidentCreateForm`, and `IncidentList`, with title `DevSupport Agent` and subtitle `Incident investigation and controlled remediation console`.

## Task 6 — Incident Route

- [ ] Add client route `app/incidents/[id]/page.tsx` using `useParams<{ id: string }>()` and rendering only `IncidentConsole`.
- [ ] Do not duplicate API, polling, or mutation logic in the route page.

## Task 7 — Incident Console State Machine

- [ ] `IncidentConsole` is the sole component owning orchestration state: `incident`, `workflow`, `report`, loading states, mutation pending state, and separately visible incident/workflow/report errors.
- [ ] On initial load, request `getIncident(id)` and then `getWorkflow(id)`. Treat only `ApiError` `404` with detail `Workflow not started` as the legal `workflow = null` empty state; render other 404s as errors.
- [ ] Show `Start Investigation` only when the exact persisted Incident is `OPEN` and workflow is absent. On click call `startWorkflow(id)`, retain its returned projection, then refresh Incident. Disable while pending.
- [ ] If start returns 503, show the safe backend detail then re-read both Incident and Workflow. The client must not guess whether the backend restored `OPEN` or retained an uncertain/existing checkpoint.
- [ ] When workflow exists and Incident is non-terminal, poll `getIncident` and `getWorkflow` every roughly 2500ms. Use an in-flight guard so requests cannot overlap; clear intervals on unmount.
- [ ] Stop workflow polling at `RESOLVED` and `NEEDS_MANUAL_ACTION`. Keep polling at `WAITING_APPROVAL`.
- [ ] On a terminal Incident, fetch final report. Also fetch promptly if `workflow.report_outcome` is non-null. A report 404 renders `Final report is not available yet.` without a retry scheduler.
- [ ] Do not optimistically set remediation/terminal status in the client.

## Task 8 — Investigation View

- [ ] Implement pure `WorkflowView`, accepting a `WorkflowResponse` only and making no API or Tool calls.
- [ ] Render current goal only when present.
- [ ] For every hypothesis render summary, status, confidence (`0.91` as `91%`, null as `—`), supporting and contradicting Evidence IDs, and next check.
- [ ] For Evidence render ID, type, source, summary, and reference; never expect or render raw Evidence data.
- [ ] Render tool history in backend array order with tool name, status, duration, public tool arguments using `<pre>{JSON.stringify(arguments, null, 2)}</pre>`, Evidence IDs, and structured error. The timeline is audit display only and cannot invoke a Tool.
- [ ] Render final conclusion when present.

## Task 9 — Action, Approval, Execution, and Verification

- [ ] In `IncidentConsole` or a small presentational section, render Proposed Action (summary, type, reason, risk, Evidence IDs) and Policy decision/reason code/reason.
- [ ] When `workflow.action` exists, render the authoritative persisted Action ID, status, service, environment, current version, target version, and reason from `workflow.action.parameters`.
- [ ] Approval wording and controls must reference only `workflow.action.parameters`, never a proposed-action parameter object.
- [ ] Render Approve and Reject controls only when all exact conditions hold: Incident `WAITING_APPROVAL`, stage `waiting_approval`, policy decision `APPROVAL_REQUIRED`, authoritative Action present, and no approval outcome.
- [ ] The buttons call only `submitApproval(id, "APPROVE")` or `submitApproval(id, "REJECT")`; disable both during mutation and immediately refresh after completion. Never add editable Action fields.
- [ ] Render execution only when present (status, service, environment, target version, executed) and verification only when present (status, summary, verification ID).

## Task 10 — Final Report

- [ ] Implement pure `FinalReportView({ report })` using the persisted `report.content` only.
- [ ] Render final status, Incident Summary, Root Cause, Timeline, Hypotheses, Key Evidence, Recommended Action, Action, Approval, Execution, and Verification.
- [ ] Show `No confirmed root cause recorded.` when root cause is absent and concise section empty states for absent Action/Approval/Execution/Verification.
- [ ] Preserve backend timeline order; do not infer or generate a second summary.
- [ ] Use one date helper based on:

  ```ts
  new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(value));
  ```

## Task 11 — Layout, CSS, and Lint

- [ ] Update layout metadata to title `DevSupport Agent` and description `Incident investigation and controlled remediation console`; keep `html lang="en"`.
- [ ] Build native CSS for a clean responsive operational console: page shell, header, panels/cards, two-column home layout, list/table, controls, buttons, status badges, error banners, empty states, timeline, and monospace IDs/tool arguments.
- [ ] Keep Incident Console max width in the 1200–1280px range and collapse to a single column on mobile. Do not add animations, glassmorphism, heavy gradients, charts, or icon libraries.
- [ ] Change the package lint script to `eslint src --ext .ts,.tsx` without upgrading Next, React, or ESLint.

## Task 12 — Static and Manual Verification

- [ ] Run from `apps/web`:

  ```powershell
  npm install
  npm run lint
  npx tsc --noEmit
  npm run build
  ```

- [ ] Confirm no manually introduced `any`:

  ```powershell
  Get-ChildItem -Recurse src -Include *.ts,*.tsx | Select-String '\bany\b'
  ```

- [ ] Confirm the only frontend backend URL is the default in `src/lib/api.ts`:

  ```powershell
  Get-ChildItem -Recurse src -Include *.ts,*.tsx | Select-String 'localhost:|127\.0\.0\.1:'
  ```

- [ ] Start `npm run dev` and verify `/`, Create Incident form, Incident List, existing Incident route, and visible backend-unavailable error state.
- [ ] If backend is available, verify list loading, create-and-route navigation, `OPEN` Start Investigation display, and that `GET Workflow not started` is a normal empty state. Leave provider-backed full Scenario A and browser E2E to Task 4.7.2.

## Acceptance Criteria

- [ ] Home supports Incident creation and list navigation.
- [ ] Detail distinguishes Incident Status from Agent Current Stage and renders hypotheses, evidence, and Tool Timeline.
- [ ] Users can start investigation; WAITING_APPROVAL exposes only authoritative Action and Approve/Reject controls.
- [ ] Execution, Recovery Verification, Final Conclusion, and persisted Final Report render when available.
- [ ] REST polling has a guarded lifecycle and stops after terminal state.
- [ ] `404 Workflow not started` is a legal empty state; 409, 503, and other API errors remain visible.
- [ ] Web contains no direct DB/Fault Lab/LLM/Tool access or backend business rules.
- [ ] Lint, strict TypeScript check, and production build pass.
- [ ] No Task 4.7.2 E2E work is included.

## Plan Self-Review

- [ ] No deferred or incomplete implementation guidance remains.
- [ ] DTO plan matches the current 4.7.0 workflow response and backend `FinalReportContent` exactly; Evidence data and proposed Action parameters remain absent.
- [ ] Approval request has only a decision and authoritative Action originates from `workflow.action`.
- [ ] Polling lifecycle, terminal stop, and 503 refresh behavior are explicit.
- [ ] No backend Python, new UI/state/test libraries, or Task 4.7.2 work is planned.

## Commit Policy

The implementation phase is one reviewer gate: after all tasks pass, create exactly one commit, `feat: build incident web console`, push `origin/main`, and stop. Do not make per-component commits.
