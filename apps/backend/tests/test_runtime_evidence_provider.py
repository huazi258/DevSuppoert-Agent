from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

import devsupport_backend.agent.nodes.tool_execution as execution_module
import devsupport_backend.workflow_console as console_module
from devsupport_backend.agent.nodes.planner import (
    PlanningError,
    deterministic_initial_evidence_plan,
    investigation_planner_node,
)
from devsupport_backend.agent.nodes.tool_execution import (
    ToolExecutionDependencies,
    tool_execution_node,
)
from devsupport_backend.agent.state import AgentStage, PendingToolCall, create_initial_agent_state
from devsupport_backend.config import Settings
from devsupport_backend.models import Incident
from devsupport_backend.tools.adapter_contracts import AdapterError
from devsupport_backend.tools.registry import ToolName
from devsupport_backend.tools.schemas import ToolStatus


class FakeLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.system_prompt: str | None = None
        self.user_prompt: str | None = None

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return self.response


def _state(description: str = "Checkout requests fail."):
    incident = Incident(
        id=uuid4(),
        service="checkout",
        environment="local",
        description=description,
        time_range_start=datetime(2026, 8, 30, tzinfo=UTC),
        time_range_end=datetime(2026, 8, 30, tzinfo=UTC) + timedelta(minutes=5),
    )
    return create_initial_agent_state(incident, symptoms=[description])


def test_runtime_evidence_provider_defaults_to_fault_lab_and_accepts_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNTIME_EVIDENCE_PROVIDER", raising=False)
    monkeypatch.delenv("DEVSUPPORT_RUNTIME_EVIDENCE_PROVIDER", raising=False)
    assert Settings().runtime_evidence_provider == "fault_lab"
    monkeypatch.setenv("RUNTIME_EVIDENCE_PROVIDER", "otel_demo")
    assert Settings().runtime_evidence_provider == "otel_demo"
    monkeypatch.delenv("RUNTIME_EVIDENCE_PROVIDER")
    monkeypatch.setenv("DEVSUPPORT_RUNTIME_EVIDENCE_PROVIDER", "otel_demo")
    assert Settings().runtime_evidence_provider == "otel_demo"


def test_default_composition_retains_every_fault_lab_read_only_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        console_module, "settings", SimpleNamespace(runtime_evidence_provider="fault_lab")
    )
    for name in (
        "FaultLabLogsAdapter",
        "FaultLabMetricsAdapter",
        "FaultLabTracesAdapter",
        "FaultLabDeploymentAdapter",
    ):
        monkeypatch.setattr(getattr(console_module, name), "from_settings", lambda: object())

    dependencies = console_module.PostgresWorkflowRuntime._tool_execution_dependencies(object())

    assert dependencies.available_tools == execution_module.READ_ONLY_INVESTIGATION_TOOLS
    assert dependencies.traces_adapter is not None
    assert dependencies.deployment_adapter is not None


def test_otel_composition_uses_only_logs_metrics_and_knowledge_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        console_module, "settings", SimpleNamespace(runtime_evidence_provider="otel_demo")
    )
    monkeypatch.setattr(console_module.OpenSearchLogsAdapter, "from_settings", lambda: object())
    monkeypatch.setattr(console_module.PrometheusMetricsAdapter, "from_settings", lambda: object())
    for name in (
        "FaultLabLogsAdapter",
        "FaultLabMetricsAdapter",
        "FaultLabTracesAdapter",
        "FaultLabDeploymentAdapter",
    ):
        monkeypatch.setattr(
            getattr(console_module, name),
            "from_settings",
            lambda: (_ for _ in ()).throw(AssertionError("Fault Lab adapter must not construct")),
        )

    dependencies = console_module.PostgresWorkflowRuntime._tool_execution_dependencies(object())

    assert dependencies.available_tools == frozenset(
        {ToolName.SEARCH_KNOWLEDGE, ToolName.QUERY_LOGS, ToolName.QUERY_METRICS}
    )
    assert dependencies.traces_adapter is None
    assert dependencies.deployment_adapter is None


def test_otel_composition_fails_early_when_real_provider_configuration_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        console_module, "settings", SimpleNamespace(runtime_evidence_provider="otel_demo")
    )
    monkeypatch.setattr(
        console_module.OpenSearchLogsAdapter,
        "from_settings",
        lambda: (_ for _ in ()).throw(AdapterError("missing_opensearch_configuration", "missing")),
    )
    monkeypatch.setattr(
        console_module.PrometheusMetricsAdapter,
        "from_settings",
        lambda: (_ for _ in ()).throw(AssertionError("must fail before metrics adapter")),
    )

    with pytest.raises(AdapterError, match="missing"):
        console_module.PostgresWorkflowRuntime._tool_execution_dependencies(object())


def test_otel_composition_fails_early_when_prometheus_configuration_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        console_module, "settings", SimpleNamespace(runtime_evidence_provider="otel_demo")
    )
    monkeypatch.setattr(console_module.OpenSearchLogsAdapter, "from_settings", lambda: object())
    monkeypatch.setattr(
        console_module.PrometheusMetricsAdapter,
        "from_settings",
        lambda: (_ for _ in ()).throw(AdapterError("missing_prometheus_configuration", "missing")),
    )

    with pytest.raises(AdapterError, match="missing"):
        console_module.PostgresWorkflowRuntime._tool_execution_dependencies(object())


def test_otel_capabilities_constrain_planner_contract_and_reject_unavailable_plan() -> None:
    available = frozenset({ToolName.SEARCH_KNOWLEDGE, ToolName.QUERY_LOGS, ToolName.QUERY_METRICS})
    state = _state()
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    client = FakeLLM(
        '{"investigation_goal":"inspect trace","tool_name":"query_traces",'
        '"tool_arguments":{"service":"checkout","environment":"local",'
        '"time_range_start":"2026-08-30T00:00:00+00:00",'
        '"time_range_end":"2026-08-30T00:05:00+00:00"},"reason":"trace"}'
    )

    with pytest.raises(PlanningError, match="unavailable tool"):
        investigation_planner_node(state, client, available)

    assert client.user_prompt is not None
    assert client.system_prompt is not None
    assert "query_traces" not in client.user_prompt
    assert "get_deployment_history" not in client.user_prompt
    assert "query_traces" not in client.system_prompt
    assert "get_deployment_history" not in client.system_prompt


def test_deterministic_initial_plan_degrades_to_logs_then_metrics_without_traces_or_deployment(
) -> None:
    available = frozenset({ToolName.SEARCH_KNOWLEDGE, ToolName.QUERY_LOGS, ToolName.QUERY_METRICS})
    state = _state("Checkout latency is high.")
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    state["tool_history"].append(
        SimpleNamespace(tool_name=ToolName.SEARCH_KNOWLEDGE)  # type: ignore[arg-type]
    )

    first = deterministic_initial_evidence_plan(state, available)
    assert first is not None
    assert first.tool_name is ToolName.QUERY_METRICS

    state["tool_history"].append(SimpleNamespace(tool_name=ToolName.QUERY_METRICS))  # type: ignore[arg-type]
    assert deterministic_initial_evidence_plan(state, available) is None


def test_deterministic_initial_plan_degrades_to_logs_then_metrics_without_deployment() -> None:
    available = frozenset({ToolName.SEARCH_KNOWLEDGE, ToolName.QUERY_LOGS, ToolName.QUERY_METRICS})
    state = _state("Checkout requests fail.")
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    state["tool_history"].append(
        SimpleNamespace(tool_name=ToolName.SEARCH_KNOWLEDGE)  # type: ignore[arg-type]
    )

    first = deterministic_initial_evidence_plan(state, available)
    assert first is not None
    assert first.tool_name is ToolName.QUERY_LOGS

    state["tool_history"].append(SimpleNamespace(tool_name=ToolName.QUERY_LOGS))  # type: ignore[arg-type]
    second = deterministic_initial_evidence_plan(state, available)
    assert second is not None
    assert second.tool_name is ToolName.QUERY_METRICS


def test_disabled_persisted_tool_is_recorded_unavailable_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    state["current_stage"] = AgentStage.TOOL_EXECUTION
    state["pending_tool_call"] = PendingToolCall(
        investigation_goal="inspect traces",
        tool_name=ToolName.QUERY_TRACES,
        tool_arguments={
            "service": "checkout",
            "environment": "local",
            "time_range_start": "2026-08-30T00:00:00+00:00",
            "time_range_end": "2026-08-30T00:05:00+00:00",
        },
        reason="persisted old plan",
    )
    dependencies = ToolExecutionDependencies(  # type: ignore[arg-type]
        rag_service=object(),
        logs_adapter=object(),
        metrics_adapter=object(),
        traces_adapter=None,
        deployment_adapter=None,
        available_tools=frozenset({ToolName.SEARCH_KNOWLEDGE, ToolName.QUERY_LOGS}),
    )
    monkeypatch.setattr(
        execution_module,
        "query_traces",
        lambda *_: (_ for _ in ()).throw(AssertionError("disabled tool must not dispatch")),
    )

    updated = tool_execution_node(state, dependencies)

    assert updated["current_stage"] is AgentStage.INVESTIGATION_PLANNING
    assert updated["tool_history"][0].status is ToolStatus.UNAVAILABLE
    assert updated["tool_history"][0].error is not None
    assert updated["tool_history"][0].error.code == "capability_unavailable"
