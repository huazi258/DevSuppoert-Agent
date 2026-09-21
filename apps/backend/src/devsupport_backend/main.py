"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from devsupport_backend.agent.persistence import (
    close_postgres_checkpointer_pool,
    initialize_postgres_checkpointer,
)
from devsupport_backend.config import settings
from devsupport_backend.routers.incidents import router as incidents_router
from devsupport_backend.routers.knowledge import router as knowledge_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize LangGraph persistence before request sessions can be checked out."""
    initialize_postgres_checkpointer()
    try:
        yield
    finally:
        close_postgres_checkpointer_pool()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type"],
)
app.include_router(incidents_router)
app.include_router(knowledge_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return the minimal liveness signal for the backend."""
    return {"status": "ok"}
