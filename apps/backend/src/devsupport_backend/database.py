"""SQLAlchemy engine and session dependency for backend domain data."""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from devsupport_backend.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """Yield a database session and ensure it is closed after use."""
    with SessionLocal() as session:
        yield session
