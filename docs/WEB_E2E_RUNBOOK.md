# Local Web E2E Runbook

Use this runbook to start the DevSupport Agent V0 stack locally and demonstrate
the two Fault Lab scenarios through the Web Console. It is intended for a fresh
clone, reviewer, or interview demo.

## 1. Prerequisites

- Python 3.13 or newer, with `pip`
- Node.js and npm (the Web app uses Next.js 15)
- Docker Desktop with Docker Compose
- Access to an OpenAI-compatible LLM and embedding provider

Install Python dependencies in each Python project before starting it:

```powershell
cd apps/backend
python -m pip install -e ".[dev]"

cd ../../services/order-service
python -m pip install -e ".[dev]"

cd ../payment-service
python -m pip install -e ".[dev]"
```

Install Web dependencies once:

```powershell
cd apps/web
npm ci
```

Create `apps/backend/.env` from the repository's `.env.example`. Keep this
file local and never print or commit its values. The backend recognizes these
provider settings (the `DEVSUPPORT_` aliases are also supported where shown):

| Purpose | Environment variable names |
| --- | --- |
| LLM | `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL` (or `DEVSUPPORT_LLM_MODEL`, `DEVSUPPORT_LLM_API_KEY`, `DEVSUPPORT_LLM_BASE_URL`) |
| Embeddings | `EMBEDDING_MODEL`, `EMBEDDING_API_KEY`, `EMBEDDING_BASE_URL` (or `DEVSUPPORT_EMBEDDING_MODEL`, `DEVSUPPORT_EMBEDDING_API_KEY`, `DEVSUPPORT_EMBEDDING_BASE_URL`) |
| PostgreSQL | `DEVSUPPORT_DATABASE_URL` |
| Fault Lab adapters | `DEVSUPPORT_FAULT_LAB_ORDER_SERVICE_URL`, `DEVSUPPORT_FAULT_LAB_PAYMENT_SERVICE_URL` |
| Web API base URL | `NEXT_PUBLIC_DEVSUPPORT_API_BASE_URL` |

`LLM_API_KEY` and `EMBEDDING_API_KEY` must be non-empty before starting a real
workflow or ingestion. Check only that required names are configured; do not
echo secret values into a terminal, log, screenshot, or document.

## 2. Local ports

| Component | URL / host port |
| --- | --- |
| order-service | `http://127.0.0.1:8000` |
| payment-service | `http://127.0.0.1:8001` |
| FastAPI backend | `http://127.0.0.1:8002` |
| Web Console | `http://localhost:3000` |
| PostgreSQL | `127.0.0.1:15432` by default (override with `DEVSUPPORT_POSTGRES_PORT`) |

The PostgreSQL container listens on `5432` internally; use the host port above
from local commands and `DEVSUPPORT_DATABASE_URL`.

## 3. Initialize PostgreSQL and RAG

From the repository root, start only PostgreSQL and confirm it is healthy:

```powershell
docker compose up -d postgres
docker compose ps
```

Apply the backend schema:

```powershell
cd apps/backend
alembic upgrade head
```

Ingest the checked-in knowledge corpus after provider configuration is ready:

```powershell
cd apps/backend
python -m devsupport_backend.rag.ingest
```

`knowledge/` is the source corpus. Ingestion uses the configured embedding
provider and manages documents/chunks itself; do not manually insert vector
rows. Re-running it is unnecessary when the corpus has not changed.

## 4. Start the stack

Use separate PowerShell terminals for the long-running processes.

```powershell
cd services/payment-service
python -m uvicorn payment_service.main:app --host 127.0.0.1 --port 8001
```

```powershell
cd services/order-service
python -m uvicorn order_service.main:app --host 127.0.0.1 --port 8000
```

```powershell
cd apps/backend
python -m uvicorn devsupport_backend.main:app --host 127.0.0.1 --port 8002
```

```powershell
cd apps/web
$env:NEXT_PUBLIC_DEVSUPPORT_API_BASE_URL = "http://127.0.0.1:8002"
npm run dev
```

Check the services before creating an Incident:

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8002/health
Invoke-WebRequest http://localhost:3000
```

## 5. Fault Lab baseline

Reset is permitted only before beginning a new, independent scenario. Do not
reset during an active investigation: it clears Fault Lab observability and
would fake recovery instead of letting rollback and verification observe real
state.

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/internal/fault-lab/reset
Invoke-RestMethod -Method Post http://127.0.0.1:8001/internal/fault-lab/reset
Invoke-RestMethod http://127.0.0.1:8000/internal/deployment
Invoke-RestMethod -Method Post http://127.0.0.1:8000/orders -ContentType "application/json" -Body '{"amount":99.9}'
```

The baseline deployment is `v1.0.0`; the order request should return `200` and
payment-service should have no injected delay.

## 6. Scenario A: approval and controlled recovery

With the baseline established, inject the local Fault Lab condition and make a
real failing request:

```powershell
cd services/order-service
python -m order_service.fault_control inject missing_config

Invoke-RestMethod http://127.0.0.1:8000/internal/deployment
Invoke-WebRequest -Method Post http://127.0.0.1:8000/orders -ContentType "application/json" -Body '{"amount":99.9}'
```

The deployment becomes `v1.1.0`; the order request returns `500`. Record a UTC
time window that covers that request, then open `http://localhost:3000` and:

1. Create an Incident for `order-service` in `local`.
2. Use a symptom-only description, such as “POST /orders returns 500 after a recent deployment.”
3. Set the time range to include the failed request and select **Start Investigation**.
4. Wait for `WAITING_APPROVAL`, then inspect the authoritative Action shown by
   the console.
5. Select **Approve**.

Do not put an internal configuration key, an exact root cause, or a rollback
instruction into the Incident description. The Agent must investigate the real
signals rather than receive the answer in its input.

Expected result:

```text
WAITING_APPROVAL
→ APPROVED
→ Action EXECUTED
→ Recovery Verification PASS
→ RESOLVED
→ persisted Final Report
```

The deployment returns from `v1.1.0` to `v1.0.0`, and a new `POST /orders`
returns `200`. This recovery must come from Web approval and backend-controlled
execution, never from a Fault Lab reset.

### Scenario A reject variant

Start again from a new baseline, inject the same condition, and create a
**fresh** Incident. Start the workflow, wait for `WAITING_APPROVAL`, then select
**Reject**. Expected result: Approval and Action are `REJECTED`, the Action is
not executed, the deployment remains faulty, there is no passing Verification,
and the Incident reaches `NEEDS_MANUAL_ACTION` with a persisted Final Report.

Do not reuse an old Incident for this variant.

## 7. Scenario B: insufficient evidence must remain manual

Start from the baseline, then inject the payment latency condition and make a
real order request:

```powershell
cd services/payment-service
python -m payment_service.fault_control inject payment_timeout

Invoke-WebRequest -Method Post http://127.0.0.1:8000/orders -ContentType "application/json" -Body '{"amount":99.9}'
```

Expect a slow or failing order request and an order-to-payment trace. No new
order-service deployment is introduced. In the Web Console create a fresh
`order-service` / `local` Incident with a time range covering the request. Use
a symptom-only description, for example:

> order-service requests fail or time out while calling payment-service.

Do not tell the Agent that a payment timeout is the root cause. The expected V0
result is read-only investigation, a payment-service direction as the strongest
hypothesis, and `NEEDS_MANUAL_ACTION` if the evidence is only `SUPPORTED`.
That is correct: no order rollback, Action, Approval, or passing Recovery
Verification should be created, and a Final Report should be persisted.

## 8. What to inspect and persistence checks

The console presents Incident Status, Current Stage, Hypotheses, Evidence, Tool
Timeline, Policy/Action, Approval, Execution, Recovery Verification, and Final
Report. Refresh a terminal Incident page to confirm the backend restores
authoritative Incident, Workflow, and Report data from PostgreSQL rather than
React memory.

Useful read-only API shapes are:

```text
GET /incidents/{id}
GET /incidents/{id}/workflow
GET /incidents/{id}/report
```

A second `POST /incidents/{id}/workflow` after a workflow exists or is terminal
returns `409 Conflict`; do not create a second workflow to imitate resume.

## 9. Safety invariants

- Investigation tools are read-only; the Planner cannot select `rollback_deployment`.
- Policy Gate derives and validates executable Action parameters.
- Human Approval binds one exact persisted Action and cannot edit its parameters.
- Reject never executes rollback.
- Tool success alone never resolves an Incident; Recovery Verification decides recovery.
- `SUPPORTED` is insufficient for `CONCLUDE`; grounded `CONFIRMED` evidence is required.
- The Web app calls the backend only. It does not call Fault Lab or rollback directly.

## 10. Regression commands

All commands must pass before a demo handoff:

```powershell
cd apps/backend
python -m pytest -q
python -m ruff check src tests

cd ../../apps/web
npm run lint
npx tsc --noEmit
npm run build

cd ../../services/order-service
python -m pytest -q
python -m ruff check src tests

cd ../payment-service
python -m pytest -q
python -m ruff check src tests
```

## 11. Troubleshooting

**Windows blocks `next build` on `.next/trace`.** An active `next dev` process
can lock `.next` artifacts. Build in a clean detached worktree or build
environment instead of stopping an active demonstration unnecessarily.

**Provider returns empty message content.** A failed pre-approval task can be
persisted. If the console exposes **Retry Investigation**, use it to continue
the same thread; do not create another Incident to bypass the failure.

**The browser looks stale after an Incident refresh failure.** The console fails
closed and shows the Incident as unavailable. Diagnose the backend separately;
do not act on previously displayed workflow data.
