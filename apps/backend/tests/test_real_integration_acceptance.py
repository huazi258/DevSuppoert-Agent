"""Pure OpenTelemetry Demo real-integration acceptance assessment tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from devsupport_backend.agent.state import (
    AgentStage,
    FailureCategory,
    HypothesisStatus,
    TerminalReason,
)
from devsupport_backend.evals.real_integration import (
    RealIntegrationAcceptanceCheck,
    RealIntegrationAcceptancePolicy,
    RealIntegrationAcceptanceStatus,
    RealIntegrationEvidenceFact,
    RealIntegrationHypothesisFact,
    RealIntegrationIncidentFacts,
    RealIntegrationPayloadSafety,
    RealIntegrationRunFacts,
    RealIntegrationSafetyFacts,
    RealIntegrationToolFact,
    RealIntegrationTrafficFacts,
    RealIntegrationUpstream,
    RealIntegrationWorkflowFacts,
    assess_real_integration_acceptance,
    collect_real_integration_run_facts,
    load_real_integration_acceptance_policy,
    write_real_integration_artifact,
)
from devsupport_backend.schemas.workflows import (
    InvestigationTimelineEventResponse,
    WorkflowEvidenceCitationResponse,
    WorkflowEvidenceResponse,
    WorkflowHypothesisResponse,
    WorkflowProgressFailureResponse,
    WorkflowProgressResponse,
    WorkflowReportOutcomeResponse,
    WorkflowResponse,
    WorkflowTimelineResponse,
    WorkflowToolHistoryResponse,
)

POLICY_PATH = Path(__file__).resolve().parents[3] / "evals" / "v1_real_integration_acceptance.yaml"


def _policy():
    return load_real_integration_acceptance_policy(POLICY_PATH)


def _facts(*, status: HypothesisStatus = HypothesisStatus.SUPPORTED) -> RealIntegrationRunFacts:
    started = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    logs_id = uuid4()
    metrics_id = uuid4()
    knowledge_id = uuid4()
    return RealIntegrationRunFacts(
        upstream=RealIntegrationUpstream(
            release="3.0.0", commit="1755859a9de82c2e5e225be68abc401a5ebf2b4f"
        ),
        scenario_id="otel_payment_failure",
        incident=RealIntegrationIncidentFacts(
            incident_id=uuid4(),
            service="checkout",
            environment="local",
            time_range_start=started,
            time_range_end=started + timedelta(minutes=1),
        ),
        traffic=RealIntegrationTrafficFacts(
            fault_window_start=started,
            fault_window_end=started + timedelta(seconds=10),
            healthy_checkout_before=True,
            checkout_attempts=10,
            checkout_http_status_counts={"500": 10},
            healthy_checkout_after=True,
            fault_restored=True,
        ),
        runtime_provider="otel_demo",
        available_tools={"search_knowledge", "query_logs", "query_metrics"},
        tool_history=[
            RealIntegrationToolFact(
                tool_name="search_knowledge", status="success", evidence_count=1
            ),
            RealIntegrationToolFact(tool_name="query_logs", status="success", evidence_count=1),
            RealIntegrationToolFact(tool_name="query_metrics", status="success", evidence_count=1),
        ],
        evidence=[
            RealIntegrationEvidenceFact(
                evidence_id=knowledge_id,
                evidence_type="knowledge_retrieval",
                source="search_knowledge",
                document_reference="rb-downstream-timeout-latency",
            ),
            RealIntegrationEvidenceFact(
                evidence_id=logs_id,
                evidence_type="log_query_result",
                source="query_logs",
            ),
            RealIntegrationEvidenceFact(
                evidence_id=metrics_id,
                evidence_type="metric_snapshot",
                source="query_metrics",
            ),
        ],
        hypotheses=[
            RealIntegrationHypothesisFact(
                hypothesis_id=uuid4(),
                summary="Checkout payment failure is caused by a downstream payment error.",
                status=status,
                supporting_evidence_ids=[logs_id, metrics_id],
            )
        ],
        workflow=RealIntegrationWorkflowFacts(
            final_status="NEEDS_MANUAL_ACTION",
            terminal_reason="investigation_inconclusive",
            report_persisted=True,
        ),
        safety=RealIntegrationSafetyFacts(
            action_count=0,
            approval_count=0,
            executed_action_count=0,
            verification_count=0,
        ),
        payload_safety=RealIntegrationPayloadSafety(),
    )


def test_supported_and_confirmed_grounded_payment_hypotheses_pass() -> None:
    assert (
        assess_real_integration_acceptance(_policy(), _facts()).status
        is RealIntegrationAcceptanceStatus.PASS
    )
    assert (
        assess_real_integration_acceptance(
            _policy(), _facts(status=HypothesisStatus.CONFIRMED)
        ).status
        is RealIntegrationAcceptanceStatus.PASS
    )


def test_knowledge_only_hypothesis_support_fails_quality_gate() -> None:
    facts = _facts()
    facts.hypotheses[0].supporting_evidence_ids = [facts.evidence[0].evidence_id]

    assessment = assess_real_integration_acceptance(_policy(), facts)

    assert assessment.status is RealIntegrationAcceptanceStatus.FAIL
    assert RealIntegrationAcceptanceCheck.INVESTIGATION_QUALITY in assessment.failed_checks


@pytest.mark.parametrize(
    "failure_category", [FailureCategory.LLM_PROVIDER_TIMEOUT, FailureCategory.LLM_PROVIDER_ERROR]
)
def test_active_hypothesis_with_typed_provider_interruption_is_blocked(
    failure_category: FailureCategory,
) -> None:
    facts = _facts(status=HypothesisStatus.ACTIVE)
    facts.timeline_interruption_categories = {failure_category}

    assessment = assess_real_integration_acceptance(_policy(), facts)

    assert assessment.status is RealIntegrationAcceptanceStatus.BLOCKED
    assert assessment.external_provider_interruption_categories == {failure_category}
    assert RealIntegrationAcceptanceCheck.INVESTIGATION_QUALITY in assessment.failed_checks


def test_active_hypothesis_without_provider_interruption_fails() -> None:
    assessment = assess_real_integration_acceptance(
        _policy(), _facts(status=HypothesisStatus.ACTIVE)
    )

    assert assessment.status is RealIntegrationAcceptanceStatus.FAIL


def test_product_workflow_failure_overrides_provider_blocker() -> None:
    facts = _facts(status=HypothesisStatus.ACTIVE)
    facts.timeline_interruption_categories = {
        FailureCategory.LLM_PROVIDER_TIMEOUT,
        FailureCategory.STRUCTURED_OUTPUT_INVALID,
    }

    assessment = assess_real_integration_acceptance(_policy(), facts)

    assert assessment.status is RealIntegrationAcceptanceStatus.FAIL
    assert RealIntegrationAcceptanceCheck.WORKFLOW_PRODUCT_FAILURE in assessment.failed_checks


@pytest.mark.parametrize(
    "mutation, expected_check",
    [
        ("missing_metrics", RealIntegrationAcceptanceCheck.RUNTIME_EVIDENCE),
        ("forbidden_tool", RealIntegrationAcceptanceCheck.FORBIDDEN_TOOLS),
        ("unexpected_tool", RealIntegrationAcceptanceCheck.PROVIDER_BOUNDARY),
        ("failed_tool", RealIntegrationAcceptanceCheck.REQUIRED_TOOLS),
        ("provider_source", RealIntegrationAcceptanceCheck.EVIDENCE_SOURCE),
        ("missing_citation", RealIntegrationAcceptanceCheck.KNOWLEDGE_CITATION),
        ("safety", RealIntegrationAcceptanceCheck.SAFETY),
        ("traffic", RealIntegrationAcceptanceCheck.SCENARIO_INTEGRITY),
        ("payload", RealIntegrationAcceptanceCheck.PAYLOAD_SAFETY),
    ],
)
def test_required_engineering_and_safety_boundaries_fail_closed(
    mutation: str, expected_check: RealIntegrationAcceptanceCheck
) -> None:
    facts = _facts()
    if mutation == "missing_metrics":
        facts.evidence.pop()
    elif mutation == "forbidden_tool":
        facts.tool_history.append(
            RealIntegrationToolFact(tool_name="query_traces", status="success", evidence_count=0)
        )
    elif mutation == "unexpected_tool":
        facts.tool_history.append(
            RealIntegrationToolFact(tool_name="unapproved_tool", status="success", evidence_count=0)
        )
    elif mutation == "failed_tool":
        facts.tool_history[1].status = "failure"
    elif mutation == "provider_source":
        facts.evidence[1].source = "opensearch"
    elif mutation == "missing_citation":
        facts.evidence[0].document_reference = None
    elif mutation == "safety":
        facts.safety.executed_action_count = 1
    elif mutation == "traffic":
        facts.traffic.fault_restored = False
    else:
        facts.payload_safety = RealIntegrationPayloadSafety(raw_logs_persisted=True)

    assessment = assess_real_integration_acceptance(_policy(), facts)

    assert assessment.status is RealIntegrationAcceptanceStatus.FAIL
    assert expected_check in assessment.failed_checks


def test_historical_m3_7_shape_is_formally_provider_blocked() -> None:
    facts = _facts(status=HypothesisStatus.ACTIVE)
    facts.hypotheses[0].supporting_evidence_ids = [facts.evidence[0].evidence_id]
    facts.timeline_interruption_categories = {FailureCategory.LLM_PROVIDER_TIMEOUT}
    facts.workflow.terminal_reason = "active_execution_budget_exhausted"

    assessment = assess_real_integration_acceptance(_policy(), facts)

    assert assessment.status is RealIntegrationAcceptanceStatus.BLOCKED
    assert assessment.diagnostics == [
        "engineering_path_passed_investigation_quality_provider_blocked"
    ]


def test_policy_is_strict_and_artifact_writer_keeps_only_safe_facts(tmp_path: Path) -> None:
    policy = _policy()
    payload = policy.model_dump(mode="json")
    payload["remediation_must_be_absent"] = False
    with pytest.raises(ValidationError):
        RealIntegrationAcceptancePolicy.model_validate(payload)

    facts = _facts()
    assessment = assess_real_integration_acceptance(policy, facts)
    artifact_path = tmp_path / "acceptance.json"
    write_real_integration_artifact(artifact_path, facts, assessment)

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["assessment"]["status"] == "PASS"
    assert '"tool_arguments":' not in artifact_path.read_text(encoding="utf-8")
    assert "raw_logs" not in artifact["run_facts"]


def test_public_projection_collector_preserves_typed_provider_failure_without_tool_input() -> None:
    facts = _facts(status=HypothesisStatus.ACTIVE)
    started = facts.incident.time_range_start
    knowledge, logs, metrics = facts.evidence
    workflow = WorkflowResponse(
        incident_id=facts.incident.incident_id,
        incident_status="NEEDS_MANUAL_ACTION",
        current_stage=AgentStage.NEEDS_MANUAL_ACTION,
        hypotheses=[
            WorkflowHypothesisResponse(
                id=facts.hypotheses[0].hypothesis_id,
                summary=facts.hypotheses[0].summary,
                status=HypothesisStatus.ACTIVE,
                confidence=None,
                supporting_evidence_ids=[logs.evidence_id],
                contradicting_evidence_ids=[],
                next_check=None,
            )
        ],
        evidence=[
            WorkflowEvidenceResponse(
                id=knowledge.evidence_id,
                evidence_type=knowledge.evidence_type,
                source=knowledge.source,
                summary="Cited knowledge",
                reference=None,
                citation=WorkflowEvidenceCitationResponse(
                    id="citation-1",
                    document_id=uuid4(),
                    chunk_id=uuid4(),
                    source="knowledge",
                    section="checkout",
                    document_reference="rb-downstream-timeout-latency",
                ),
            ),
            WorkflowEvidenceResponse(
                id=logs.evidence_id,
                evidence_type=logs.evidence_type,
                source=logs.source,
                summary="Runtime logs",
                reference=None,
            ),
            WorkflowEvidenceResponse(
                id=metrics.evidence_id,
                evidence_type=metrics.evidence_type,
                source=metrics.source,
                summary="Runtime metrics",
                reference=None,
            ),
        ],
        tool_history=[
            WorkflowToolHistoryResponse(
                tool_name="search_knowledge",
                tool_arguments={"query": "must not enter acceptance artifact"},
                status="success",
                duration_ms=1.0,
                evidence_ids=[knowledge.evidence_id],
                error=None,
            ),
            WorkflowToolHistoryResponse(
                tool_name="query_logs",
                tool_arguments={"query": "must not enter acceptance artifact"},
                status="success",
                duration_ms=1.0,
                evidence_ids=[logs.evidence_id],
                error=None,
            ),
            WorkflowToolHistoryResponse(
                tool_name="query_metrics",
                tool_arguments={"service": "checkout"},
                status="success",
                duration_ms=1.0,
                evidence_ids=[metrics.evidence_id],
                error=None,
            ),
        ],
        current_goal=None,
        final_conclusion=None,
        proposed_action=None,
        policy_outcome=None,
        action=None,
        approval_outcome=None,
        execution_outcome=None,
        verification_outcome=None,
        report_outcome=WorkflowReportOutcomeResponse(
            report_id=uuid4(),
            incident_id=facts.incident.incident_id,
            final_status="NEEDS_MANUAL_ACTION",
        ),
        terminal_reason=TerminalReason.INVESTIGATION_INCONCLUSIVE,
    )
    progress = WorkflowProgressResponse(
        incident_id=facts.incident.incident_id,
        incident_status="NEEDS_MANUAL_ACTION",
        phase="completed",
        checkpoint_available=True,
        current_stage=AgentStage.NEEDS_MANUAL_ACTION,
        current_goal=None,
        pending_tool_name=None,
        hypothesis_count=1,
        evidence_count=3,
        tool_call_count=3,
        investigation_round=1,
        llm_call_count=1,
        workflow_retry_count=0,
        latest_tool=None,
        failure=WorkflowProgressFailureResponse(
            failed_node="hypothesis_update",
            category=FailureCategory.LLM_PROVIDER_TIMEOUT,
            message="safe timeout",
            retryable=True,
        ),
        terminal_reason=TerminalReason.INVESTIGATION_INCONCLUSIVE,
    )
    timeline = WorkflowTimelineResponse(
        incident_id=facts.incident.incident_id,
        checkpoint_available=True,
        events=[
            InvestigationTimelineEventResponse(
                event_id="interrupted:1",
                sequence=1,
                event_type="investigation_interrupted",
                occurred_at=started,
                title="Provider interrupted",
                summary="Safe historical interruption",
                status=FailureCategory.LLM_PROVIDER_TIMEOUT,
            )
        ],
    )

    collected = collect_real_integration_run_facts(
        upstream=facts.upstream,
        scenario_id=facts.scenario_id,
        incident=facts.incident,
        traffic=facts.traffic,
        runtime_provider=facts.runtime_provider,
        available_tools=facts.available_tools,
        workflow=workflow,
        progress=progress,
        timeline=timeline,
    )

    assert collected.timeline_interruption_categories == {FailureCategory.LLM_PROVIDER_TIMEOUT}
    assert all("must not enter" not in item.model_dump_json() for item in collected.tool_history)
    assert (
        assess_real_integration_acceptance(_policy(), collected).status
        is RealIntegrationAcceptanceStatus.BLOCKED
    )
