"""PostgreSQL-backed LangGraph checkpointer lifecycle helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool
from sqlalchemy.engine import make_url

from devsupport_backend.config import settings

_setup_lock = Lock()
_setup_complete = False
_pool_lock = Lock()
_checkpointer_pool: ConnectionPool | None = None


def psycopg_dsn(database_url: str) -> str:
    """Convert the application's SQLAlchemy PostgreSQL URL for psycopg safely."""
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("LangGraph persistence requires a PostgreSQL database URL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def open_postgres_checkpointer() -> Iterator[PostgresSaver]:
    """Yield a saver whose individual checkpoint calls borrow pooled connections.

    A workflow can wait on LLMs and read-only providers for a long time.  The
    saver therefore owns a psycopg pool rather than one connection for the
    whole graph invocation; ``PostgresSaver`` checks a connection out only
    while it executes a checkpoint query or write.
    """
    checkpointer = PostgresSaver(_get_checkpointer_pool())
    _ensure_checkpointer_setup(checkpointer)
    yield checkpointer


def initialize_postgres_checkpointer() -> None:
    """Complete checkpointer DDL before FastAPI begins serving requests."""
    with open_postgres_checkpointer():
        pass


def close_postgres_checkpointer_pool() -> None:
    """Return dedicated checkpointer connections during application shutdown."""
    global _checkpointer_pool, _setup_complete
    with _pool_lock:
        if _checkpointer_pool is not None:
            _checkpointer_pool.close()
            _checkpointer_pool = None
    with _setup_lock:
        _setup_complete = False


def _get_checkpointer_pool() -> ConnectionPool:
    global _checkpointer_pool
    with _pool_lock:
        if _checkpointer_pool is None:
            _checkpointer_pool = ConnectionPool(
                conninfo=psycopg_dsn(settings.database_url),
                min_size=1,
                max_size=5,
                kwargs={"autocommit": True, "prepare_threshold": 0},
                open=True,
            )
        return _checkpointer_pool


def _ensure_checkpointer_setup(checkpointer: PostgresSaver) -> None:
    """Run LangGraph schema setup once per backend process to avoid concurrent DDL."""
    global _setup_complete
    with _setup_lock:
        if _setup_complete:
            return
        checkpointer.setup()
        _setup_complete = True
