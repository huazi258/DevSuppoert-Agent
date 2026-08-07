"""FastAPI application entry point."""

from fastapi import FastAPI

from devsupport_backend.config import settings
from devsupport_backend.routers.incidents import router as incidents_router

app = FastAPI(title=settings.app_name)
app.include_router(incidents_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return the minimal liveness signal for the backend."""
    return {"status": "ok"}
