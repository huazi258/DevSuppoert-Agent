"""Read-only, bounded diagnosis of the saved real-integration workflow history."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from pydantic import Field

from devsupport_backend.agent.runtime import WorkflowCheckpointHistory
from devsupport_backend.agent.state import AgentStage, AgentState, EvidenceContext, TerminalReason
from devsupport_backend.database import SessionLocal
from devsupport_backend.evals.contracts import EvalModel
from devsupport_backend.models import Incident
from devsupport_backend.workflow_console import PostgresWorkflowRuntime

DEFAULT_INCIDENT_ID = UUID("b58dbfcd-36b8-4c6b-8c58-09e4f6a08fc9")
DEFAULT_ARTIFACT_PATH = (
    Path(__file__).resolve().parents[5]
    / "evals"
    / "results"
    / "v1-m5.3-investigation-quality-diagnosis.json"
)


class InvestigationQualityRootCause(StrEnum):
    EVIDENCE_NOT_COLLECTED = "evidence_not_collected"
    EVIDENCE_NOT_SPECIFIC_ENOUGH = "evidence_not_specific_enough"
    EVIDENCE_NOT_CONSUMED_BEFORE_BUDGET_TERMINAL = "evidence_not_consumed_before_budget_terminal"
    HYPOTHESIS_UPDATE_DID_NOT_BIND_RELEVANT_EVIDENCE = (
        "hypothesis_update_did_not_bind_relevant_evidence"
    )
    INSUFFICIENT_HISTORICAL_EVIDENCE_TO_DETERMINE = "insufficient_historical_evidence_to_determine"


class DiagnosisToolFact(EvalModel):
    tool_name: str = Field(min_length=1, max_length=100)
    status: str = Field(min_length=1, max_length=50)
    target_service: str | None = Field(default=None, min_length=1, max_length=100)
    duration_ms: float | None = Field(default=None, ge=0)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)


class DiagnosisMetricFacts(EvalModel):
    request_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    error_rate: float = Field(ge=0)
    average_request_duration_ms: float | None = Field(default=None, ge=0)


class DiagnosisLogPattern(EvalModel):
    pattern: str = Field(min_length=1, max_length=250)
    count: int = Field(ge=0)


class DiagnosisLogFacts(EvalModel):
    match_count: int = Field(ge=0)
    error_patterns: list[DiagnosisLogPattern] = Field(default_factory=list, max_length=10)
    trace_id_count: int = Field(ge=0, le=20)


class DiagnosisEvidenceFact(EvalModel):
    evidence_id: UUID
    source: str = Field(min_length=1, max_length=100)
    evidence_type: str = Field(min_length=1, max_length=100)
    service: str | None = Field(default=None, min_length=1, max_length=100)
    metrics: DiagnosisMetricFacts | None = None
    logs: DiagnosisLogFacts | None = None


class DiagnosisHypothesisChange(EvalModel):
    hypothesis_id: UUID
    summary: str = Field(min_length=1, max_length=2_000)
    before_status: str | None = Field(default=None, min_length=1, max_length=50)
    after_status: str = Field(min_length=1, max_length=50)
    before_confidence: float | None = Field(default=None, ge=0, le=1)
    after_confidence: float | None = Field(default=None, ge=0, le=1)
    new_supporting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    new_contradicting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)


class DiagnosisFrame(EvalModel):
    sequence: int = Field(ge=1)
    created_at: datetime
    current_stage: AgentStage
    investigation_round: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    llm_call_count: int = Field(ge=0)
    active_execution_seconds: float = Field(ge=0)
    terminal_reason: TerminalReason | None = None
    new_tool_calls: list[DiagnosisToolFact] = Field(default_factory=list, max_length=10)
    new_evidence: list[DiagnosisEvidenceFact] = Field(default_factory=list, max_length=100)
    hypothesis_changes: list[DiagnosisHypothesisChange] = Field(
        default_factory=list, max_length=100
    )


class EvidenceConsumptionFact(EvalModel):
    evidence_id: UUID
    source: str = Field(min_length=1, max_length=100)
    evidence_type: str = Field(min_length=1, max_length=100)
    service: str | None = Field(default=None, min_length=1, max_length=100)
    produced_sequence: int = Field(ge=1)
    completed_hypothesis_update_after_production: bool
    final_hypothesis_ids: list[UUID] = Field(default_factory=list, max_length=100)


class RuntimeEvidenceContractFacts(EvalModel):
    logs_evidence_includes_queried_service: bool
    normalized_log_event_includes_downstream_service: bool
    log_error_patterns_have_structured_downstream_identity: bool
    metrics_evidence_includes_service: bool
    hypothesis_update_receives_all_evidence: bool
    hypothesis_update_receives_only_latest_tool_history: bool


class RealIntegrationDiagnosisArtifact(EvalModel):
    source_m5_3_incident_id: UUID
    checkpoint_history_available: bool
    checkpoint_history_truncated: bool
    frames: list[DiagnosisFrame] = Field(default_factory=list, max_length=200)
    evidence_consumption: list[EvidenceConsumptionFact] = Field(
        default_factory=list, max_length=200
    )
    runtime_evidence_contract: RuntimeEvidenceContractFacts
    findings: list[str] = Field(default_factory=list, max_length=30)
    primary_root_cause: InvestigationQualityRootCause
    contributing_factors: list[str] = Field(default_factory=list, max_length=20)
    recommended_fix_boundary: str = Field(min_length=1, max_length=2_000)


def diagnose_real_integration_history(
    incident_id: UUID, history: WorkflowCheckpointHistory
) -> RealIntegrationDiagnosisArtifact:
    """Project persisted checkpoints without retaining arguments or provider payloads."""
    if not history.records:
        return _unavailable_history_artifact(incident_id, history.truncated)

    frames = _diagnostic_frames(history)
    consumption = _evidence_consumption(frames, history.records[-1].state)
    final_state = history.records[-1].state
    terminal_reason = _terminal_reason(final_state)
    root_cause = classify_primary_root_cause(
        checkpoint_history_available=True,
        incident_service=final_state["incident"].service,
        runtime_evidence=consumption,
        terminal_reason=terminal_reason,
    )
    findings = _findings(frames, consumption, root_cause, terminal_reason)
    return RealIntegrationDiagnosisArtifact(
        source_m5_3_incident_id=incident_id,
        checkpoint_history_available=True,
        checkpoint_history_truncated=history.truncated,
        frames=frames,
        evidence_consumption=consumption,
        runtime_evidence_contract=_runtime_evidence_contract(),
        findings=findings,
        primary_root_cause=root_cause,
        contributing_factors=_contributing_factors(root_cause),
        recommended_fix_boundary=_recommended_fix_boundary(root_cause),
    )


def classify_primary_root_cause(
    *,
    checkpoint_history_available: bool,
    incident_service: str,
    runtime_evidence: list[EvidenceConsumptionFact],
    terminal_reason: TerminalReason | None,
) -> InvestigationQualityRootCause:
    """Classify the first proven boundary that prevents runtime evidence attribution."""
    if not checkpoint_history_available:
        return InvestigationQualityRootCause.INSUFFICIENT_HISTORICAL_EVIDENCE_TO_DETERMINE
    if not runtime_evidence:
        return InvestigationQualityRootCause.EVIDENCE_NOT_COLLECTED
    relevant = [
        item
        for item in runtime_evidence
        if item.service is not None and item.service != incident_service
    ]
    if not relevant:
        return InvestigationQualityRootCause.EVIDENCE_NOT_SPECIFIC_ENOUGH
    if terminal_reason is TerminalReason.ACTIVE_EXECUTION_BUDGET_EXHAUSTED and any(
        not item.completed_hypothesis_update_after_production for item in relevant
    ):
        return InvestigationQualityRootCause.EVIDENCE_NOT_CONSUMED_BEFORE_BUDGET_TERMINAL
    if all(item.completed_hypothesis_update_after_production for item in relevant) and all(
        not item.final_hypothesis_ids for item in relevant
    ):
        return InvestigationQualityRootCause.HYPOTHESIS_UPDATE_DID_NOT_BIND_RELEVANT_EVIDENCE
    return InvestigationQualityRootCause.INSUFFICIENT_HISTORICAL_EVIDENCE_TO_DETERMINE


def write_real_integration_diagnosis_artifact(
    path: Path, artifact: RealIntegrationDiagnosisArtifact
) -> None:
    path.write_text(
        json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _diagnostic_frames(history: WorkflowCheckpointHistory) -> list[DiagnosisFrame]:
    frames: list[DiagnosisFrame] = []
    previous_state: AgentState | None = None
    for sequence, record in enumerate(history.records, start=1):
        state = record.state
        frames.append(
            DiagnosisFrame(
                sequence=sequence,
                created_at=record.created_at,
                current_stage=state["current_stage"],
                investigation_round=state["investigation_round"],
                tool_call_count=state["tool_call_count"],
                llm_call_count=state["llm_call_count"],
                active_execution_seconds=state["active_execution_seconds"],
                terminal_reason=_terminal_reason(state),
                new_tool_calls=_new_tool_calls(previous_state, state),
                new_evidence=_new_evidence(previous_state, state),
                hypothesis_changes=_hypothesis_changes(previous_state, state),
            )
        )
        previous_state = state
    return frames


def _new_tool_calls(previous: AgentState | None, current: AgentState) -> list[DiagnosisToolFact]:
    prior_count = len(previous["tool_history"]) if previous is not None else 0
    return [
        DiagnosisToolFact(
            tool_name=entry.tool_name.value,
            status=entry.status.value,
            target_service=_service_argument(entry.tool_arguments),
            duration_ms=entry.duration_ms,
            evidence_ids=entry.evidence_ids,
        )
        for entry in current["tool_history"][prior_count:]
    ]


def _new_evidence(previous: AgentState | None, current: AgentState) -> list[DiagnosisEvidenceFact]:
    prior_ids = {item.id for item in previous["evidence"]} if previous is not None else set()
    return [_diagnosis_evidence(item) for item in current["evidence"] if item.id not in prior_ids]


def _diagnosis_evidence(evidence: EvidenceContext) -> DiagnosisEvidenceFact:
    source = evidence.source
    data = evidence.data
    if source == "query_metrics":
        metrics = data.get("metrics")
        if isinstance(metrics, dict):
            service = metrics.get("service")
            if (
                isinstance(service, str)
                and isinstance(metrics.get("request_count"), int)
                and isinstance(metrics.get("error_count"), int)
                and isinstance(metrics.get("error_rate"), (float, int))
            ):
                average = metrics.get("average_request_duration_ms")
                return DiagnosisEvidenceFact(
                    evidence_id=evidence.id,
                    source=source,
                    evidence_type=evidence.evidence_type,
                    service=service,
                    metrics=DiagnosisMetricFacts(
                        request_count=metrics["request_count"],
                        error_count=metrics["error_count"],
                        error_rate=float(metrics["error_rate"]),
                        average_request_duration_ms=(
                            float(average) if isinstance(average, (float, int)) else None
                        ),
                    ),
                )
    if source == "query_logs":
        patterns = data.get("error_patterns")
        normalized_patterns = (
            [
                DiagnosisLogPattern(pattern=entry["pattern"], count=entry["count"])
                for entry in patterns
                if isinstance(entry, dict)
                and isinstance(entry.get("pattern"), str)
                and isinstance(entry.get("count"), int)
            ]
            if isinstance(patterns, list)
            else []
        )
        match_count = data.get("match_count")
        trace_ids = data.get("trace_ids")
        return DiagnosisEvidenceFact(
            evidence_id=evidence.id,
            source=source,
            evidence_type=evidence.evidence_type,
            logs=DiagnosisLogFacts(
                match_count=match_count if isinstance(match_count, int) else 0,
                error_patterns=normalized_patterns,
                trace_id_count=len(trace_ids) if isinstance(trace_ids, list) else 0,
            ),
        )
    return DiagnosisEvidenceFact(
        evidence_id=evidence.id,
        source=source,
        evidence_type=evidence.evidence_type,
    )


def _hypothesis_changes(
    previous: AgentState | None, current: AgentState
) -> list[DiagnosisHypothesisChange]:
    prior = {item.id: item for item in previous["hypotheses"]} if previous is not None else {}
    changes: list[DiagnosisHypothesisChange] = []
    for item in current["hypotheses"]:
        before = prior.get(item.id)
        new_supporting = [
            evidence_id
            for evidence_id in item.supporting_evidence_ids
            if before is None or evidence_id not in before.supporting_evidence_ids
        ]
        new_contradicting = [
            evidence_id
            for evidence_id in item.contradicting_evidence_ids
            if before is None or evidence_id not in before.contradicting_evidence_ids
        ]
        if (
            before is None
            or before.status != item.status
            or before.confidence != item.confidence
            or new_supporting
            or new_contradicting
        ):
            changes.append(
                DiagnosisHypothesisChange(
                    hypothesis_id=item.id,
                    summary=item.summary,
                    before_status=before.status.value if before is not None else None,
                    after_status=item.status.value,
                    before_confidence=before.confidence if before is not None else None,
                    after_confidence=item.confidence,
                    new_supporting_evidence_ids=new_supporting,
                    new_contradicting_evidence_ids=new_contradicting,
                )
            )
    return changes


def _evidence_consumption(
    frames: list[DiagnosisFrame], final_state: AgentState
) -> list[EvidenceConsumptionFact]:
    final_bindings: dict[UUID, list[UUID]] = {}
    for hypothesis in final_state["hypotheses"]:
        for evidence_id in [
            *hypothesis.supporting_evidence_ids,
            *hypothesis.contradicting_evidence_ids,
        ]:
            final_bindings.setdefault(evidence_id, []).append(hypothesis.id)
    consumption: list[EvidenceConsumptionFact] = []
    for frame in frames:
        for evidence in frame.new_evidence:
            if evidence.source not in {"query_logs", "query_metrics"}:
                continue
            consumption.append(
                EvidenceConsumptionFact(
                    evidence_id=evidence.evidence_id,
                    source=evidence.source,
                    evidence_type=evidence.evidence_type,
                    service=evidence.service,
                    produced_sequence=frame.sequence,
                    completed_hypothesis_update_after_production=any(
                        later.current_stage is AgentStage.EVIDENCE_EVALUATION
                        and later.investigation_round > frame.investigation_round
                        for later in frames[frame.sequence :]
                    ),
                    final_hypothesis_ids=final_bindings.get(evidence.evidence_id, []),
                )
            )
    return consumption


def _service_argument(arguments: dict[str, object]) -> str | None:
    service = arguments.get("service")
    return service if isinstance(service, str) and service else None


def _terminal_reason(state: AgentState) -> TerminalReason | None:
    value = state.get("terminal_reason")
    return TerminalReason(value) if isinstance(value, str) else value


def _runtime_evidence_contract() -> RuntimeEvidenceContractFacts:
    return RuntimeEvidenceContractFacts(
        logs_evidence_includes_queried_service=False,
        normalized_log_event_includes_downstream_service=False,
        log_error_patterns_have_structured_downstream_identity=False,
        metrics_evidence_includes_service=True,
        hypothesis_update_receives_all_evidence=True,
        hypothesis_update_receives_only_latest_tool_history=True,
    )


def _findings(
    frames: list[DiagnosisFrame],
    consumption: list[EvidenceConsumptionFact],
    root_cause: InvestigationQualityRootCause,
    terminal_reason: TerminalReason | None,
) -> list[str]:
    findings: list[str] = [
        f"runtime_evidence_count={len(consumption)}",
        f"terminal_reason={terminal_reason.value if terminal_reason else 'none'}",
    ]
    logs = next(
        (
            evidence.logs
            for frame in frames
            for evidence in frame.new_evidence
            if evidence.source == "query_logs"
        ),
        None,
    )
    checkout_metrics = next(
        (
            evidence.metrics
            for frame in frames
            for evidence in frame.new_evidence
            if evidence.source == "query_metrics" and evidence.service == "checkout"
        ),
        None,
    )
    if logs is not None and checkout_metrics is not None:
        findings.append(
            "initial_logs_and_checkout_metrics_do_not_distinguish_checkout_failure_from_"
            "payment_downstream_failure"
        )
    payment_metrics = [
        item for item in consumption if item.source == "query_metrics" and item.service == "payment"
    ]
    if payment_metrics:
        item = payment_metrics[-1]
        findings.append(
            "payment_metrics_produced_at_sequence="
            f"{item.produced_sequence};completed_hypothesis_update_after_production="
            f"{str(item.completed_hypothesis_update_after_production).lower()}"
        )
    findings.append(f"primary_root_cause={root_cause.value}")
    return findings


def _contributing_factors(root_cause: InvestigationQualityRootCause) -> list[str]:
    factors = [
        "logs_runtime_evidence_does_not_retain_queried_or_downstream_service_identity",
        "hypothesis_update_receives_all_evidence_but_only_latest_tool_history",
    ]
    if root_cause is InvestigationQualityRootCause.EVIDENCE_NOT_CONSUMED_BEFORE_BUDGET_TERMINAL:
        factors.insert(0, "active_execution_budget_terminalized_after_relevant_evidence_production")
    return factors


def _recommended_fix_boundary(root_cause: InvestigationQualityRootCause) -> str:
    if root_cause is InvestigationQualityRootCause.EVIDENCE_NOT_CONSUMED_BEFORE_BUDGET_TERMINAL:
        return (
            "Investigate active-execution budget timing at the post-tool to hypothesis-update "
            "boundary before changing Agent prompts or hypothesis binding."
        )
    if root_cause is InvestigationQualityRootCause.HYPOTHESIS_UPDATE_DID_NOT_BIND_RELEVANT_EVIDENCE:
        return (
            "Investigate hypothesis-update evidence binding after preserving the current planner, "
            "tool, and budget boundaries."
        )
    if root_cause is InvestigationQualityRootCause.EVIDENCE_NOT_SPECIFIC_ENOUGH:
        return (
            "Investigate evaluator-only runtime-evidence specificity before changing Agent "
            "behavior."
        )
    if root_cause is InvestigationQualityRootCause.EVIDENCE_NOT_COLLECTED:
        return (
            "Investigate the planned runtime-evidence collection boundary before changing Agent "
            "behavior."
        )
    return (
        "Preserve Agent behavior and add evaluator-only checkpoint observability before another "
        "real-integration run."
    )


def _unavailable_history_artifact(
    incident_id: UUID, truncated: bool
) -> RealIntegrationDiagnosisArtifact:
    root_cause = InvestigationQualityRootCause.INSUFFICIENT_HISTORICAL_EVIDENCE_TO_DETERMINE
    return RealIntegrationDiagnosisArtifact(
        source_m5_3_incident_id=incident_id,
        checkpoint_history_available=False,
        checkpoint_history_truncated=truncated,
        runtime_evidence_contract=_runtime_evidence_contract(),
        findings=["checkpoint_history_unavailable"],
        primary_root_cause=root_cause,
        contributing_factors=[],
        recommended_fix_boundary=_recommended_fix_boundary(root_cause),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose saved M5.3 checkpoint history")
    parser.add_argument("--incident-id", type=UUID, default=DEFAULT_INCIDENT_ID)
    parser.add_argument("--output", type=Path, default=DEFAULT_ARTIFACT_PATH)
    args = parser.parse_args()
    with SessionLocal() as session:
        incident = session.get(Incident, args.incident_id)
        if incident is None or not incident.thread_id:
            history = WorkflowCheckpointHistory(records=())
        else:
            history = PostgresWorkflowRuntime(session).get_checkpoint_history(incident.thread_id)
    artifact = diagnose_real_integration_history(args.incident_id, history)
    write_real_integration_diagnosis_artifact(args.output, artifact)
    print(json.dumps({"primary_root_cause": artifact.primary_root_cause}, ensure_ascii=False))


if __name__ == "__main__":
    main()
