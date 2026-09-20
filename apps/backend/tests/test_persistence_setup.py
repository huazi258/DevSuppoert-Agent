"""Regression tests for bounded PostgreSQL checkpointer setup."""

from devsupport_backend.agent import persistence


class _FakeCheckpointer:
    def __init__(self) -> None:
        self.setup_calls = 0

    def setup(self) -> None:
        self.setup_calls += 1


def test_checkpointer_schema_setup_runs_once_per_backend_process(monkeypatch) -> None:
    first = _FakeCheckpointer()
    second = _FakeCheckpointer()
    monkeypatch.setattr(persistence, "_setup_complete", False)

    persistence._ensure_checkpointer_setup(first)  # noqa: SLF001
    persistence._ensure_checkpointer_setup(second)  # noqa: SLF001

    assert first.setup_calls == 1
    assert second.setup_calls == 0
