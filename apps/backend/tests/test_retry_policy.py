"""V2 runtime retry and fallback policy tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from devsupport_backend.agent.budget import InvestigationBudget
from devsupport_backend.agent.state import AgentStage, ToolHistoryEntry, create_initial_agent_state
from devsupport_backend.agent.workflow import _investigation_planning_node
from devsupport_backend.models import Incident
from devsupport_backend.tools.registry import ToolName
from devsupport_backend.tools.schemas import ToolError, ToolStatus


class _FallbackPlanner:
    def __init__(self, *, tool_name: str = "query_logs", environment: str = "local") -> None:
        self.system_prompt = ""
        self._tool_name = tool_name
        self._environment = environment

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        del user_prompt
        self.system_prompt = system_prompt
        return json.dumps(
            {
                "investigation_goal": "Check bounded error logs.",
                "tool_name": self._tool_name,
                "tool_arguments": self._tool_arguments(),
                "reason": "Logs remain a legal independent investigation path.",
            }
        )

    def _tool_arguments(self) -> dict[str, str]:
        if self._tool_name == "query_metrics":
            return {"service": "order-service", "environment": self._environment}
        return {
            "service": "order-service",
            "environment": self._environment,
            "time_range_start": "2026-08-08T10:00:00+00:00",
            "time_range_end": "2026-08-08T10:05:00+00:00",
        }


def _planning_state() -> dict[str, object]:
    started_at = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    incident = Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        description="订单接口持续返回 500。",
        time_range_start=started_at,
        time_range_end=started_at + timedelta(minutes=5),
    )
    state = create_initial_agent_state(incident)
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    return state


def test_invalid_request_allows_planner_to_select_the_same_tool_with_new_arguments() -> None:
    state = _planning_state()
    state["tool_history"] = [
        ToolHistoryEntry(
            tool_name=ToolName.QUERY_METRICS,
            tool_arguments={"service": "order-service", "environment": "local"},
            status=ToolStatus.FAILURE,
            error=ToolError(code="invalid_request", message="safe invalid request"),
        )
    ]
    planner = _FallbackPlanner(tool_name="query_metrics", environment="staging")

    updated = _investigation_planning_node(
        state,  # type: ignore[arg-type]
        planner,
        InvestigationBudget(),
        frozenset({ToolName.QUERY_METRICS, ToolName.QUERY_LOGS}),
    )

    assert updated["pending_tool_call"] is not None
    assert updated["pending_tool_call"].tool_name is ToolName.QUERY_METRICS
    assert updated["pending_tool_call"].tool_arguments["environment"] == "staging"
    assert "query_metrics" in planner.system_prompt


def test_capability_unavailable_removes_that_tool_before_planner_falls_back() -> None:
    state = _planning_state()
    state["tool_history"] = [
        ToolHistoryEntry(
            tool_name=ToolName.QUERY_METRICS,
            tool_arguments={"service": "order-service", "environment": "local"},
            status=ToolStatus.UNAVAILABLE,
            error=ToolError(
                code="capability_unavailable", message="safe unavailable capability"
            ),
        )
    ]
    planner = _FallbackPlanner()

    updated = _investigation_planning_node(
        state,  # type: ignore[arg-type]
        planner,
        InvestigationBudget(),
        frozenset({ToolName.QUERY_METRICS, ToolName.QUERY_LOGS}),
    )

    assert updated["pending_tool_call"] is not None
    assert updated["pending_tool_call"].tool_name is ToolName.QUERY_LOGS
    assert "query_logs" in planner.system_prompt
    assert "query_metrics" not in planner.system_prompt


def test_no_remaining_legal_capability_ends_with_no_further_investigation() -> None:
    state = _planning_state()
    state["tool_history"] = [
        ToolHistoryEntry(
            tool_name=ToolName.QUERY_METRICS,
            tool_arguments={"service": "order-service", "environment": "local"},
            status=ToolStatus.UNAVAILABLE,
            error=ToolError(
                code="capability_unavailable", message="safe unavailable capability"
            ),
        ),
        ToolHistoryEntry(
            tool_name=ToolName.QUERY_LOGS,
            tool_arguments={"service": "order-service", "environment": "local"},
            status=ToolStatus.UNAVAILABLE,
            error=ToolError(
                code="capability_unavailable", message="safe unavailable capability"
            ),
        ),
    ]

    updated = _investigation_planning_node(
        state,  # type: ignore[arg-type]
        _FallbackPlanner(),
        InvestigationBudget(),
        frozenset({ToolName.QUERY_METRICS, ToolName.QUERY_LOGS}),
    )

    assert updated["pending_tool_call"] is None
    assert updated["current_stage"] is AgentStage.INVESTIGATION_PLANNING
    assert updated["terminal_reason"].value == "no_further_investigation"
