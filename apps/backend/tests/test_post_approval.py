"""Shared post-approval graph routing tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from langgraph.graph import START, StateGraph

from devsupport_backend.agent.post_approval import add_post_approval_continuation
from devsupport_backend.agent.state import (
    ActionExecutionOutcome,
    AgentStage,
    AgentState,
    ReportOutcome,
    create_initial_agent_state,
)
from devsupport_backend.tools.schemas import ToolStatus


class FailingExecution:
    def execute(self, _state):
        return ActionExecutionOutcome(status=ToolStatus.FAILURE, executed=False)


class UnexpectedVerification:
    def verify(self, _state):
        raise AssertionError("execution failure must not enter recovery verification")


class RecordingReport:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, state):
        self.calls += 1
        return ReportOutcome(
            report_id=uuid4(),
            incident_id=state["incident"].id,
            final_status="NEEDS_MANUAL_ACTION",
        )


def test_execution_failure_routes_to_final_report_when_verification_exists() -> None:
    now = datetime.now(UTC)
    incident = SimpleNamespace(
        id=uuid4(),
        service="order-service",
        environment="local",
        description="Topology test incident.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=1),
    )
    state = create_initial_agent_state(incident)
    state["current_stage"] = AgentStage.ACTION_EXECUTION
    report = RecordingReport()
    graph = StateGraph(AgentState)
    graph.add_node("approval_decision", lambda current: current)
    graph.add_edge(START, "approval_decision")
    add_post_approval_continuation(
        graph,
        action_execution=FailingExecution(),
        recovery_verification=UnexpectedVerification(),
        final_report=report,
    )

    result = graph.compile().invoke(state)

    assert result["current_stage"] is AgentStage.NEEDS_MANUAL_ACTION
    assert result["report_outcome"] is not None
    assert report.calls == 1
