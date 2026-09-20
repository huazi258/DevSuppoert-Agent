"""Safe V2-only OpenTelemetry Demo Logs and Metrics integration acceptance."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.adapter_runtime import ProviderBackendConfigRegistry, TargetAdapterResolver
from devsupport_backend.agent.nodes.tool_execution import tool_execution_node
from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    PendingToolCall,
    TerminalReason,
    create_initial_agent_state,
)
from devsupport_backend.agent.v2_terminalization import V2Terminalizer
from devsupport_backend.config import ProviderBackendConfig
from devsupport_backend.database import SessionLocal
from devsupport_backend.investigation_lifecycle import InvestigationLifecycleService
from devsupport_backend.investigation_status import InvestigationStatus
from devsupport_backend.models import (
    Incident,
    InvestigationRound,
    InvestigationTarget,
    Report,
    Service,
)
from devsupport_backend.target_config import (
    AdapterType,
    CapabilityConfig,
    InvestigationTargetConfig,
    ProviderConfig,
    ProviderConfigRegistry,
    TargetServiceConfig,
)
from devsupport_backend.tools.registry import ToolName, v2_tool_registry
from devsupport_backend.tools.schemas import ToolStatus

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_RESULT_PATH = REPOSITORY_ROOT / "evals" / "results" / "v2-otel-integration.json"
PINNED_OTEL_RELEASE = "3.0.0"
PINNED_OTEL_COMMIT = "1755859a9de82c2e5e225be68abc401a5ebf2b4f"
INTEGRATION_TARGET_SLUG = "otel-demo-v2-integration"
INTEGRATION_SERVICE = "checkout"
INTEGRATION_ENVIRONMENT = "local"
SIDE_EFFECT_TERMS = ("rollback", "restart", "action", "approval", "recovery")
PROVIDER_BLOCKED_CODES = frozenset(
    {"provider_unavailable", "timeout", "opensearch_unavailable", "prometheus_unavailable"}
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class V2IntegrationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class AdapterAcceptanceStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    BLOCKED = "blocked"


class V2ModelProviderStatus(StrEnum):
    NOT_INVOKED = "not_invoked"
    BLOCKED = "blocked"


class V2OtelExpectedUpstream(_Model):
    release: str = PINNED_OTEL_RELEASE
    commit: str = PINNED_OTEL_COMMIT


class V2OtelObservedUpstream(_Model):
    release: str | None = None
    commit: str | None = None
    identity_verified: bool | None = None


class V2OtelFaultScenario(_Model):
    fault_name: str | None = None
    enabled: bool | None = None
    restored: bool | None = None
    checkout_attempts: int | None = Field(default=None, ge=0)
    checkout_http_status_counts: dict[str, int] | None = None
    non_2xx_observed: bool | None = None


class V2OtelScenarioFacts(_Model):
    expected_upstream: V2OtelExpectedUpstream = Field(default_factory=V2OtelExpectedUpstream)
    observed_upstream: V2OtelObservedUpstream = Field(default_factory=V2OtelObservedUpstream)
    fault: V2OtelFaultScenario = Field(default_factory=V2OtelFaultScenario)


class V2OtelTargetIdentity(_Model):
    slug: str = INTEGRATION_TARGET_SLUG
    service: str = INTEGRATION_SERVICE
    environment: str = INTEGRATION_ENVIRONMENT


class V2AdapterAcceptance(_Model):
    status: AdapterAcceptanceStatus
    normalized_evidence_count: int = Field(ge=0)
    normalized_source: str | None = None
    safe_error_code: str | None = None


class V2ProviderPayloadSafety(_Model):
    provider_endpoint_exposed: bool = False
    credential_exposed: bool = False
    raw_provider_payload_exposed: bool = False


class V2OtelIntegrationFacts(_Model):
    scenario: V2OtelScenarioFacts
    target: V2OtelTargetIdentity
    available_tools: list[str]
    logs_adapter: V2AdapterAcceptance
    metrics_adapter: V2AdapterAcceptance
    normalized_runtime_evidence_count: int = Field(ge=0)
    final_status: InvestigationStatus
    terminal_reason: str | None = None
    report_persisted: bool
    concluded_grounding_verified: bool | None = None
    side_effect_tool_count: int = Field(ge=0)
    provider_payload_safety: V2ProviderPayloadSafety
    external_model_provider_status: V2ModelProviderStatus = V2ModelProviderStatus.NOT_INVOKED
    blockers: list[str] = Field(default_factory=list)


class V2OtelIntegrationAssessment(_Model):
    status: V2IntegrationStatus
    failed_checks: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


def assess_v2_otel_integration(facts: V2OtelIntegrationFacts) -> V2OtelIntegrationAssessment:
    """Assess only V2 adapter, persistence, and safety facts; never V1 remediation."""
    blockers = [*facts.blockers, *_missing_scenario_facts(facts.scenario)]
    if facts.external_model_provider_status is V2ModelProviderStatus.BLOCKED:
        blockers.append("external_model_provider_blocked")
    if any(
        adapter.status is AdapterAcceptanceStatus.BLOCKED
        for adapter in (facts.logs_adapter, facts.metrics_adapter)
    ):
        blockers.append("runtime_adapter_unavailable")
    if blockers:
        return V2OtelIntegrationAssessment(
            status=V2IntegrationStatus.BLOCKED, blockers=sorted(set(blockers))
        )

    failed_checks: list[str] = []
    if facts.scenario.observed_upstream.identity_verified is not True:
        failed_checks.append("upstream_identity")
    if facts.scenario.observed_upstream.release != facts.scenario.expected_upstream.release:
        failed_checks.append("upstream_release")
    if facts.scenario.observed_upstream.commit != facts.scenario.expected_upstream.commit:
        failed_checks.append("upstream_commit")
    if facts.scenario.fault.fault_name != "paymentFailure":
        failed_checks.append("fault_name")
    if facts.scenario.fault.enabled is not True:
        failed_checks.append("fault_enabled")
    if facts.scenario.fault.restored is not True:
        failed_checks.append("fault_restored")
    if (facts.scenario.fault.checkout_attempts or 0) < 1:
        failed_checks.append("checkout_attempts")
    if not _checkout_status_counts_match_attempts(facts.scenario):
        failed_checks.append("checkout_status_counts")
    if not facts.scenario.fault.non_2xx_observed or not _has_non_2xx(facts.scenario):
        failed_checks.append("non_2xx_observed")
    if (
        facts.target.service != INTEGRATION_SERVICE
        or facts.target.environment != INTEGRATION_ENVIRONMENT
    ):
        failed_checks.append("target_identity")
    if set(facts.available_tools) != {"search_knowledge", "query_logs", "query_metrics"}:
        failed_checks.append("capability_allowlist")
    if facts.logs_adapter.status is not AdapterAcceptanceStatus.SUCCESS:
        failed_checks.append("logs_adapter")
    if (
        facts.logs_adapter.normalized_evidence_count < 1
        or facts.logs_adapter.normalized_source != "opensearch"
    ):
        failed_checks.append("logs_normalization")
    if facts.metrics_adapter.status is not AdapterAcceptanceStatus.SUCCESS:
        failed_checks.append("metrics_adapter")
    if (
        facts.metrics_adapter.normalized_evidence_count < 1
        or facts.metrics_adapter.normalized_source != "prometheus"
    ):
        failed_checks.append("metrics_normalization")
    if facts.normalized_runtime_evidence_count < 2:
        failed_checks.append("runtime_evidence")
    if facts.final_status not in {
        InvestigationStatus.CONCLUDED,
        InvestigationStatus.INCONCLUSIVE,
        InvestigationStatus.FAILED,
    }:
        failed_checks.append("safe_terminal_status")
    if (
        facts.final_status is InvestigationStatus.CONCLUDED
        and not facts.concluded_grounding_verified
    ):
        failed_checks.append("concluded_grounding")
    if not facts.report_persisted:
        failed_checks.append("report_persisted")
    if facts.side_effect_tool_count != 0:
        failed_checks.append("side_effect_tools")
    if any(facts.provider_payload_safety.model_dump().values()):
        failed_checks.append("provider_payload_safety")
    return V2OtelIntegrationAssessment(
        status=V2IntegrationStatus.FAIL if failed_checks else V2IntegrationStatus.PASS,
        failed_checks=failed_checks,
    )


def run_v2_otel_integration(
    session: Session,
    *,
    opensearch_url: str,
    prometheus_url: str,
    scenario: V2OtelScenarioFacts,
) -> V2OtelIntegrationFacts:
    """Exercise the configured V2 resolver against real OTel Logs and Metrics endpoints."""
    target_record, service_record = _ensure_target_and_service(session)
    target = _target_config(target_record.id)
    resolver = _resolver(opensearch_url=opensearch_url, prometheus_url=prometheus_url)
    incident, round_record = _start_integration_round(session, target_record, service_record)
    dependencies = resolver.build_tool_execution_dependencies(target, object())
    state = create_initial_agent_state(incident)
    state["round_id"] = round_record.id

    logs_state = _execute_tool(
        state,
        dependencies,
        ToolName.QUERY_LOGS,
        {
            "service": INTEGRATION_SERVICE,
            "environment": INTEGRATION_ENVIRONMENT,
            "time_range_start": incident.time_range_start.isoformat(),
            "time_range_end": incident.time_range_end.isoformat(),
            "limit": 20,
        },
    )
    metrics_state = _execute_tool(
        logs_state,
        dependencies,
        ToolName.QUERY_METRICS,
        {"service": INTEGRATION_SERVICE, "environment": INTEGRATION_ENVIRONMENT},
    )
    logs = _adapter_acceptance(logs_state, ToolName.QUERY_LOGS)
    metrics = _adapter_acceptance(metrics_state, ToolName.QUERY_METRICS)
    terminal_status = (
        InvestigationStatus.INCONCLUSIVE
        if logs.status is AdapterAcceptanceStatus.SUCCESS
        and metrics.status is AdapterAcceptanceStatus.SUCCESS
        else InvestigationStatus.FAILED
    )
    terminal_state = {
        **metrics_state,
        "terminal_reason": (
            TerminalReason.INVESTIGATION_INCONCLUSIVE
            if terminal_status is InvestigationStatus.INCONCLUSIVE
            else TerminalReason.WORKFLOW_FAILURE
        ),
    }
    terminalized = V2Terminalizer(session).terminalize(terminal_state, terminal_status)
    report_id = terminalized["report_outcome"].report_id
    persisted_report = session.get(Report, report_id)
    runtime_evidence = [
        item for item in terminalized["evidence"] if item.source in {"query_logs", "query_metrics"}
    ]
    safety = V2ProviderPayloadSafety(
        provider_endpoint_exposed=_contains_provider_endpoint(terminalized),
        raw_provider_payload_exposed=_contains_raw_payload_marker(terminalized),
    )
    return V2OtelIntegrationFacts(
        scenario=scenario,
        target=V2OtelTargetIdentity(),
        available_tools=sorted(tool.value for tool in dependencies.available_tools),
        logs_adapter=logs,
        metrics_adapter=metrics,
        normalized_runtime_evidence_count=len(runtime_evidence),
        final_status=terminal_status,
        terminal_reason=terminal_state["terminal_reason"].value,
        report_persisted=(
            persisted_report is not None and persisted_report.round_id == round_record.id
        ),
        side_effect_tool_count=_side_effect_tool_count(),
        provider_payload_safety=safety,
    )


def write_v2_otel_integration_artifact(
    path: Path,
    facts: V2OtelIntegrationFacts,
    assessment: V2OtelIntegrationAssessment,
) -> None:
    """Write a safe projection excluding endpoints, credentials, and Tool inputs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "integration": facts.model_dump(mode="json"),
                "assessment": assessment.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _ensure_target_and_service(session: Session) -> tuple[InvestigationTarget, Service]:
    target = session.scalar(
        select(InvestigationTarget).where(InvestigationTarget.slug == INTEGRATION_TARGET_SLUG)
    )
    if target is None:
        target = InvestigationTarget(
            name="OpenTelemetry Demo V2 integration",
            slug=INTEGRATION_TARGET_SLUG,
            description="Acceptance-only configured target for the pinned OpenTelemetry Demo.",
            environment=INTEGRATION_ENVIRONMENT,
            enabled=True,
        )
        session.add(target)
        session.flush()
    service = session.scalar(
        select(Service).where(Service.target_id == target.id, Service.name == INTEGRATION_SERVICE)
    )
    if service is None:
        service = Service(
            target_id=target.id,
            name=INTEGRATION_SERVICE,
            display_name="Checkout",
            description="Pinned OpenTelemetry Demo checkout service.",
            enabled=True,
        )
        session.add(service)
        session.flush()
    session.commit()
    return target, service


def _target_config(target_id: UUID) -> InvestigationTargetConfig:
    return InvestigationTargetConfig(
        target_id=target_id,
        slug=INTEGRATION_TARGET_SLUG,
        environment=INTEGRATION_ENVIRONMENT,
        services=(TargetServiceConfig(name=INTEGRATION_SERVICE),),
        logs=CapabilityConfig(
            enabled=True,
            adapter_type=AdapterType.OPENSEARCH,
            provider_config_ref="otel-demo-logs",
        ),
        metrics=CapabilityConfig(
            enabled=True,
            adapter_type=AdapterType.PROMETHEUS,
            provider_config_ref="otel-demo-metrics",
        ),
    )


def _resolver(*, opensearch_url: str, prometheus_url: str) -> TargetAdapterResolver:
    return TargetAdapterResolver(
        ProviderConfigRegistry(
            [
                ProviderConfig(
                    provider_config_ref="otel-demo-logs",
                    adapter_type=AdapterType.OPENSEARCH,
                    backend_config_key="otel-demo-logs-backend",
                ),
                ProviderConfig(
                    provider_config_ref="otel-demo-metrics",
                    adapter_type=AdapterType.PROMETHEUS,
                    backend_config_key="otel-demo-metrics-backend",
                ),
            ]
        ),
        ProviderBackendConfigRegistry(
            [
                ProviderBackendConfig(
                    backend_config_key="otel-demo-logs-backend",
                    adapter_type=AdapterType.OPENSEARCH,
                    endpoint=opensearch_url,
                ),
                ProviderBackendConfig(
                    backend_config_key="otel-demo-metrics-backend",
                    adapter_type=AdapterType.PROMETHEUS,
                    endpoint=prometheus_url,
                ),
            ]
        ),
    )


def _start_integration_round(
    session: Session, target: InvestigationTarget, service: Service
) -> tuple[Incident, InvestigationRound]:
    now = datetime.now(UTC)
    incident = Incident(
        target_id=target.id,
        service_id=service.id,
        service=service.name,
        environment=INTEGRATION_ENVIRONMENT,
        description="OpenTelemetry Demo payment-failure adapter integration acceptance.",
        time_range_start=now - timedelta(minutes=15),
        time_range_end=now,
        thread_id=f"v2-otel-integration-{uuid4()}",
        investigation_status=InvestigationStatus.OPEN,
    )
    round_record = InvestigationRound(
        incident=incident,
        round_number=1,
        status=InvestigationStatus.OPEN,
        thread_id=f"v2-otel-integration-round-{uuid4()}",
    )
    session.add_all((incident, round_record))
    session.commit()
    InvestigationLifecycleService(session).start(round_record.id)
    return incident, round_record


def _execute_tool(
    state: AgentState,
    dependencies: object,
    tool_name: ToolName,
    arguments: dict[str, object],
) -> AgentState:
    next_state = {
        **state,
        "current_stage": AgentStage.TOOL_EXECUTION,
        "pending_tool_call": PendingToolCall(
            investigation_goal="收集受控的 OpenTelemetry Demo 运行时证据。",
            tool_name=tool_name,
            tool_arguments=arguments,
            reason="V2 adapter integration acceptance",
        ),
    }
    return tool_execution_node(next_state, dependencies)  # type: ignore[arg-type]


def _adapter_acceptance(state: AgentState, tool_name: ToolName) -> V2AdapterAcceptance:
    tool_history = [item for item in state["tool_history"] if item.tool_name is tool_name]
    entry = tool_history[-1]
    evidence = [item for item in state["evidence"] if item.source == tool_name.value]
    if entry.status is ToolStatus.SUCCESS:
        source = evidence[0].data.get("provenance", {}).get("source") if evidence else None
        return V2AdapterAcceptance(
            status=AdapterAcceptanceStatus.SUCCESS,
            normalized_evidence_count=len(evidence),
            normalized_source=source,
        )
    error_code = entry.error.code if entry.error is not None else "tool_failure"
    return V2AdapterAcceptance(
        status=(
            AdapterAcceptanceStatus.BLOCKED
            if error_code in PROVIDER_BLOCKED_CODES
            else AdapterAcceptanceStatus.FAILURE
        ),
        normalized_evidence_count=0,
        safe_error_code=error_code,
    )


def _missing_scenario_facts(scenario: V2OtelScenarioFacts) -> list[str]:
    observed = scenario.observed_upstream
    fault = scenario.fault
    missing = []
    if observed.release is None or observed.commit is None or observed.identity_verified is None:
        missing.append("scenario_upstream_identity_unavailable")
    if fault.fault_name is None or fault.enabled is None or fault.restored is None:
        missing.append("scenario_fault_state_unavailable")
    if fault.checkout_attempts is None or fault.checkout_http_status_counts is None:
        missing.append("scenario_checkout_observation_unavailable")
    if fault.non_2xx_observed is None:
        missing.append("scenario_non_2xx_observation_unavailable")
    return missing


def _has_non_2xx(scenario: V2OtelScenarioFacts) -> bool:
    counts = scenario.fault.checkout_http_status_counts or {}
    return any(not status.startswith("2") and count > 0 for status, count in counts.items())


def _checkout_status_counts_match_attempts(scenario: V2OtelScenarioFacts) -> bool:
    attempts = scenario.fault.checkout_attempts
    counts = scenario.fault.checkout_http_status_counts
    return attempts is not None and counts is not None and sum(counts.values()) == attempts


def _side_effect_tool_count() -> int:
    return sum(
        any(term in definition.name.value for term in SIDE_EFFECT_TERMS)
        for definition in v2_tool_registry.list()
    )


def _contains_provider_endpoint(state: AgentState) -> bool:
    serialized = _safe_state_json(state)
    return "http://" in serialized or "https://" in serialized


def _contains_raw_payload_marker(state: AgentState) -> bool:
    serialized = _safe_state_json(state).lower()
    return "raw_payload" in serialized or "raw_response" in serialized


def _safe_state_json(state: AgentState) -> str:
    def serialize(value: object) -> object:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        return str(value)

    return json.dumps(state, default=serialize)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run V2 OpenTelemetry Demo adapter integration acceptance"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULT_PATH)
    args = parser.parse_args()
    opensearch_url = os.environ.get("DEVSUPPORT_OTEL_DEMO_OPENSEARCH_URL")
    prometheus_url = os.environ.get("DEVSUPPORT_OTEL_DEMO_PROMETHEUS_URL")
    scenario = _scenario_from_environment()
    if not opensearch_url or not prometheus_url:
        facts = V2OtelIntegrationFacts(
            scenario=scenario,
            target=V2OtelTargetIdentity(),
            available_tools=[],
            logs_adapter=V2AdapterAcceptance(status="blocked", normalized_evidence_count=0),
            metrics_adapter=V2AdapterAcceptance(status="blocked", normalized_evidence_count=0),
            normalized_runtime_evidence_count=0,
            final_status=InvestigationStatus.FAILED,
            report_persisted=False,
            side_effect_tool_count=_side_effect_tool_count(),
            provider_payload_safety=V2ProviderPayloadSafety(),
            blockers=["otel_demo_endpoint_configuration_missing"],
        )
    else:
        with SessionLocal() as session:
            facts = run_v2_otel_integration(
                session,
                opensearch_url=opensearch_url,
                prometheus_url=prometheus_url,
                scenario=scenario,
            )
    assessment = assess_v2_otel_integration(facts)
    write_v2_otel_integration_artifact(args.output, facts, assessment)
    print(json.dumps(assessment.model_dump(mode="json"), ensure_ascii=False))
    if assessment.status is not V2IntegrationStatus.PASS:
        raise SystemExit(1)


def _scenario_from_environment() -> V2OtelScenarioFacts:
    return V2OtelScenarioFacts(
        observed_upstream=V2OtelObservedUpstream(
            release=os.environ.get("DEVSUPPORT_OTEL_UPSTREAM_RELEASE"),
            commit=os.environ.get("DEVSUPPORT_OTEL_UPSTREAM_COMMIT"),
            identity_verified=_environment_bool("DEVSUPPORT_OTEL_UPSTREAM_IDENTITY_VERIFIED"),
        ),
        fault=V2OtelFaultScenario(
            fault_name=os.environ.get("DEVSUPPORT_OTEL_FAULT_NAME"),
            enabled=_environment_bool("DEVSUPPORT_OTEL_FAULT_ENABLED"),
            restored=_environment_bool("DEVSUPPORT_OTEL_FAULT_RESTORED"),
            checkout_attempts=_environment_int("DEVSUPPORT_OTEL_CHECKOUT_ATTEMPTS"),
            checkout_http_status_counts=_environment_status_counts(),
            non_2xx_observed=_environment_bool("DEVSUPPORT_OTEL_NON_2XX_OBSERVED"),
        ),
    )


def _environment_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None:
        return None
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return None


def _environment_int(name: str) -> int | None:
    value = os.environ.get(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _environment_status_counts() -> dict[str, int] | None:
    raw_counts = os.environ.get("DEVSUPPORT_OTEL_CHECKOUT_HTTP_STATUS_COUNTS")
    try:
        decoded = json.loads(raw_counts) if raw_counts else None
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    if any(
        not isinstance(status, str)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        for status, count in decoded.items()
    ):
        return None
    return decoded


if __name__ == "__main__":
    main()
