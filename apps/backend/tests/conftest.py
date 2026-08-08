"""Shared database fixtures for tests that need an empty knowledge corpus."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from devsupport_backend.database import engine
from devsupport_backend.models import KnowledgeChunk, KnowledgeDocument


@pytest.fixture
def database_session() -> Iterator[Session]:
    """Provide an empty knowledge view while restoring the real corpus on rollback."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        session.execute(delete(KnowledgeChunk))
        session.execute(delete(KnowledgeDocument))
        session.flush()
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
