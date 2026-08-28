"""Tests for optional, content-free V1 investigation timing hooks."""

from __future__ import annotations

import pytest

from devsupport_backend.agent.observability import (
    active_investigation_node,
    observe_investigation_node,
)
from devsupport_backend.evals.runner import LLMObservability, ObservedLLMClient


class _NodeEvents:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def node_started(self, node_name: str) -> None:
        self.events.append(("started", node_name))

    def node_finished(self, node_name: str, duration_ms: float, outcome: str) -> None:
        self.events.append(("finished", node_name, duration_ms, outcome))


class _LLMEvents:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def llm_call_started(self, call_id: int, node_name: str | None) -> None:
        self.events.append(("started", call_id, node_name))

    def llm_call_finished(
        self,
        call_id: int,
        node_name: str | None,
        duration_ms: float,
        outcome: str,
    ) -> None:
        self.events.append(("finished", call_id, node_name, duration_ms, outcome))


def test_node_timing_records_repeated_completed_calls_and_resets_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter((10.0, 10.1, 11.0, 11.25))
    monkeypatch.setattr(
        "devsupport_backend.agent.observability.perf_counter", lambda: next(timestamps)
    )
    events = _NodeEvents()

    def node(_: object) -> str:
        assert active_investigation_node() == "investigation_planning"
        return "unchanged workflow result"

    observed = observe_investigation_node("investigation_planning", node, events)

    assert observed({}) == "unchanged workflow result"  # type: ignore[arg-type]
    assert observed({}) == "unchanged workflow result"  # type: ignore[arg-type]
    assert active_investigation_node() is None
    assert events.events == [
        ("started", "investigation_planning"),
        ("finished", "investigation_planning", 100.0, "completed"),
        ("started", "investigation_planning"),
        ("finished", "investigation_planning", 250.0, "completed"),
    ]


def test_node_and_llm_exceptions_are_recorded_without_context_leak() -> None:
    node_events = _NodeEvents()
    llm_events = _LLMEvents()

    class FailingDelegate:
        def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            assert active_investigation_node() == "hypothesis_generation"
            raise RuntimeError("provider unavailable")

    llm_client = ObservedLLMClient(
        FailingDelegate(),
        LLMObservability(call_observer=llm_events),
    )

    def node(_: object) -> None:
        llm_client.complete(system_prompt="trusted", user_prompt="context")

    observed = observe_investigation_node("hypothesis_generation", node, node_events)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        observed({})  # type: ignore[arg-type]

    assert active_investigation_node() is None
    assert node_events.events[0] == ("started", "hypothesis_generation")
    assert node_events.events[-1][0:2] == ("finished", "hypothesis_generation")
    assert node_events.events[-1][-1] == "error"
    assert llm_events.events[0] == ("started", 1, "hypothesis_generation")
    assert llm_events.events[-1][0:3] == ("finished", 1, "hypothesis_generation")
    assert llm_events.events[-1][-1] == "error"


def test_observer_failures_do_not_change_node_or_llm_execution() -> None:
    class FailingNodeObserver:
        def node_started(self, node_name: str) -> None:
            raise RuntimeError(node_name)

        def node_finished(self, node_name: str, duration_ms: float, outcome: str) -> None:
            raise RuntimeError(node_name)

    class FailingLLMObserver:
        def llm_call_started(self, call_id: int, node_name: str | None) -> None:
            raise RuntimeError(str(call_id))

        def llm_call_finished(
            self,
            call_id: int,
            node_name: str | None,
            duration_ms: float,
            outcome: str,
        ) -> None:
            raise RuntimeError(str(call_id))

    class Delegate:
        def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            return "unchanged completion"

    llm_client = ObservedLLMClient(
        Delegate(),
        LLMObservability(call_observer=FailingLLMObserver()),
    )
    observed = observe_investigation_node(
        "resolution_proposal",
        lambda _: llm_client.complete(system_prompt="trusted", user_prompt="context"),
        FailingNodeObserver(),
    )

    assert observed({}) == "unchanged completion"  # type: ignore[arg-type]
    assert active_investigation_node() is None
