"""FastAPI application entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from devsupport_backend.config import settings
from devsupport_backend.routers.incidents import router as incidents_router

app = FastAPI(title=settings.app_name)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)
app.include_router(incidents_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return the minimal liveness signal for the backend."""
    return {"status": "ok"}
