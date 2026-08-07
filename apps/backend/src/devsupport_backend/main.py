"""FastAPI application entry point."""

from fastapi import FastAPI

from devsupport_backend.config import settings

app = FastAPI(title=settings.app_name)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return the minimal liveness signal for the backend."""
    return {"status": "ok"}
