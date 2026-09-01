"""Regression coverage for the evaluator-only M5.3 checkpoint diagnosis."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from devsupport_backend.agent.runtime import WorkflowCheckpointHistory, WorkflowCheckpointRecord
from devsupport_backend.agent.state import (
    AgentStage,
    EvidenceContext,
    HypothesisContext,
    HypothesisStatus,
    TerminalReason,
    ToolHistoryEntry,
    create_initial_agent_state,
)
from devsupport_backend.evals.real_integration_diagnosis import (
    EvidenceConsumptionFact,
    InvestigationQualityRootCause,
    RealIntegrationDiagnosisArtifact,
    classify_primary_root_cause,
    diagnose_real_integration_history,
    write_real_integration_diagnosis_artifact,
)
from devsupport_backend.tools.registry import ToolName
from devsupport_backend.tools.schemas import ToolStatus


def _incident() -> SimpleNamespace:
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    return SimpleNamespace(
        id=uuid4(),
        service="checkout",
        environment="local",
        description="Checkout failures during payment calls.",
        time_range_start=now,
        time_range_end=now,
    )


def _history(*, bind_payment_evidence: bool = False) -> tuple[WorkflowCheckpointHistory, UUID]:
    incident = _incident()
    payment_evidence_id = uuid4()
    payment_evidence = EvidenceContext(
        id=payment_evidence_id,
        evidence_type="metric_snapshot",
        source="query_metrics",
        summary="Payment metrics are available.",
        data={
            "metrics": {
                "service": "payment",
                "request_count": 10,
                "error_count": 10,
                "error_rate": 1.0,
                "average_request_duration_ms": 50.0,
                "provider_payload": "must-not-persist",
            }
        },
    )
    log_evidence = EvidenceContext(
        id=uuid4(),
        evidence_type="log_query_result",
        source="query_logs",
        summary="Logs are available.",
        data={
            "match_count": 1,
            "error_patterns": [{"pattern": "bounded timeout", "count": 1}],
            "trace_ids": ["trace-1"],
            "raw_log_message": "must-not-persist",
        },
    )
    hypothesis = HypothesisContext(
        id=uuid4(),
        summary="Payment is the downstream failure source.",
        status=HypothesisStatus.ACTIVE,
        supporting_evidence_ids=[payment_evidence_id] if bind_payment_evidence else [],
    )
    produced = create_initial_agent_state(incident)
    produced.update(
        {
            "current_stage": AgentStage.HYPOTHESIS_UPDATE,
            "investigation_round": 1,
            "tool_call_count": 2,
            "llm_call_count": 2,
            "active_execution_seconds": 20.0,
            "tool_history": [
                ToolHistoryEntry(
                    tool_name=ToolName.QUERY_LOGS,
                    tool_arguments={"service": "checkout", "query": "must-not-persist"},
                    status=ToolStatus.SUCCESS,
                    duration_ms=5.0,
                    evidence_ids=[log_evidence.id],
                ),
                ToolHistoryEntry(
                    tool_name=ToolName.QUERY_METRICS,
                    tool_arguments={"service": "payment", "query": "must-not-persist"},
                    status=ToolStatus.SUCCESS,
                    duration_ms=6.0,
                    evidence_ids=[payment_evidence_id],
                ),
            ],
            "evidence": [log_evidence, payment_evidence],
            "hypotheses": [hypothesis],
        }
    )
    consumed = {
        **produced,
        "current_stage": AgentStage.EVIDENCE_EVALUATION,
        "investigation_round": 2,
    }
    if bind_payment_evidence:
        consumed["hypotheses"] = [
            hypothesis.model_copy(update={"status": HypothesisStatus.SUPPORTED})
        ]
    history = WorkflowCheckpointHistory(
        records=(
            WorkflowCheckpointRecord(
                state=produced, created_at=datetime(2026, 8, 31, 12, tzinfo=UTC)
            ),
            WorkflowCheckpointRecord(
                state=consumed, created_at=datetime(2026, 8, 31, 12, 1, tzinfo=UTC)
            ),
        )
    )
    return history, incident.id


def _runtime_evidence(
    *, service: str | None, consumed: bool, bound: bool
) -> EvidenceConsumptionFact:
    return EvidenceConsumptionFact(
        evidence_id=uuid4(),
        source="query_metrics",
        evidence_type="metric_snapshot",
        service=service,
        produced_sequence=1,
        completed_hypothesis_update_after_production=consumed,
        final_hypothesis_ids=[uuid4()] if bound else [],
    )


@pytest.mark.parametrize(
    ("history_available", "runtime_evidence", "terminal_reason", "expected"),
    [
        (True, [], None, InvestigationQualityRootCause.EVIDENCE_NOT_COLLECTED),
        (
            True,
            [_runtime_evidence(service="checkout", consumed=True, bound=False)],
            None,
            InvestigationQualityRootCause.EVIDENCE_NOT_SPECIFIC_ENOUGH,
        ),
        (
            True,
            [_runtime_evidence(service="payment", consumed=False, bound=False)],
            TerminalReason.ACTIVE_EXECUTION_BUDGET_EXHAUSTED,
            InvestigationQualityRootCause.EVIDENCE_NOT_CONSUMED_BEFORE_BUDGET_TERMINAL,
        ),
        (
            True,
            [_runtime_evidence(service="payment", consumed=True, bound=False)],
            None,
            InvestigationQualityRootCause.HYPOTHESIS_UPDATE_DID_NOT_BIND_RELEVANT_EVIDENCE,
        ),
        (
            False,
            [_runtime_evidence(service="payment", consumed=False, bound=False)],
            TerminalReason.ACTIVE_EXECUTION_BUDGET_EXHAUSTED,
            InvestigationQualityRootCause.INSUFFICIENT_HISTORICAL_EVIDENCE_TO_DETERMINE,
        ),
    ],
)
def test_root_cause_classifier_covers_collection_specificity_consumption_and_binding(
    history_available: bool,
    runtime_evidence: list[EvidenceConsumptionFact],
    terminal_reason: TerminalReason | None,
    expected: InvestigationQualityRootCause,
) -> None:
    assert (
        classify_primary_root_cause(
            checkpoint_history_available=history_available,
            incident_service="checkout",
            runtime_evidence=runtime_evidence,
            terminal_reason=terminal_reason,
        )
        is expected
    )


def test_diagnosis_projection_keeps_only_bounded_safe_facts(tmp_path) -> None:
    history, incident_id = _history()

    artifact = diagnose_real_integration_history(incident_id, history)
    output = tmp_path / "diagnosis.json"
    write_real_integration_diagnosis_artifact(output, artifact)

    payload = output.read_text(encoding="utf-8")
    loaded = RealIntegrationDiagnosisArtifact.model_validate(json.loads(payload))
    assert loaded.checkpoint_history_available is True
    assert (
        loaded.primary_root_cause
        is InvestigationQualityRootCause.HYPOTHESIS_UPDATE_DID_NOT_BIND_RELEVANT_EVIDENCE
    )
    assert loaded.frames[0].new_tool_calls[1].target_service == "payment"
    assert loaded.frames[0].new_evidence[0].service is None
    assert loaded.frames[0].new_evidence[1].service == "payment"
    assert "tool_arguments" not in payload
    assert "must-not-persist" not in payload
    assert "provider_payload" not in payload


def test_evidence_consumption_marks_a_completed_hypothesis_update_and_binding() -> None:
    history, incident_id = _history(bind_payment_evidence=True)

    artifact = diagnose_real_integration_history(incident_id, history)

    payment = next(item for item in artifact.evidence_consumption if item.service == "payment")
    assert payment.completed_hypothesis_update_after_production is True
    assert payment.final_hypothesis_ids
