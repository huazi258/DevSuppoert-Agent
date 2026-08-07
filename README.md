# DevSupport Agent V0

DevSupport Agent is a controlled microservice incident investigation and remediation agent for engineering teams.

## Current status

Day 1 / Task 1.1 repository foundation: the backend health endpoint and minimal web shell are available. Agent, RAG, fault scenarios, and operational tooling are intentionally not implemented yet.

## Documentation

- [Product requirements](docs/PRD.md)
- [Technical design](docs/TECH_DESIGN.md)
- [Implementation plan](docs/IMPLEMENTATION_PLAN.md)

## Run locally

### Backend

```powershell
cd apps/backend
python -m pip install -e ".[dev]"
uvicorn devsupport_backend.main:app --reload
```

The health endpoint is available at `http://127.0.0.1:8000/health`.

### Frontend

```powershell
cd apps/web
npm install
npm run dev
```

Open `http://localhost:3000`.
