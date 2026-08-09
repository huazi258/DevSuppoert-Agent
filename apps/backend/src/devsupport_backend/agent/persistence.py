"""PostgreSQL-backed LangGraph checkpointer lifecycle helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy.engine import make_url

from devsupport_backend.config import settings


def psycopg_dsn(database_url: str) -> str:
    """Convert the application's SQLAlchemy PostgreSQL URL for psycopg safely."""
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("LangGraph persistence requires a PostgreSQL database URL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def open_postgres_checkpointer() -> Iterator[PostgresSaver]:
    """Open and initialize the official saver using the sole application database setting."""
    with PostgresSaver.from_conn_string(psycopg_dsn(settings.database_url)) as checkpointer:
        checkpointer.setup()
        yield checkpointer
