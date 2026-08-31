"""Deterministic acceptance assessment for one OpenTelemetry Demo integration run."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

import yaml
from pydantic import Field, field_validator, model_validator

from devsupport_backend.agent.state import FailureCategory, HypothesisStatus
from devsupport_backend.evals.contracts import EvalModel
from devsupport_backend.schemas.workflows import (
    WorkflowProgressResponse,
    WorkflowResponse,
    WorkflowTimelineResponse,
)

_PINNED_OTEL_RELEASE = "3.0.0"
_PINNED_OTEL_COMMIT = "1755859a9de82c2e5e225be68abc401a5ebf2b4f"
_ALLOWED_RUNTIME_TOOLS = {"search_knowledge", "query_logs", "query_metrics"}
_EXTERNAL_PROVIDER_FAILURES = {
    FailureCategory.LLM_PROVIDER_TIMEOUT,
    FailureCategory.LLM_PROVIDER_ERROR,
}


class RealIntegrationAcceptanceStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class RealIntegrationAcceptanceCheck(StrEnum):
    SCENARIO_INTEGRITY = "scenario_integrity"
    PROVIDER_BOUNDARY = "provider_boundary"
    REQUIRED_TOOLS = "required_tools"
    FORBIDDEN_TOOLS = "forbidden_tools"
    RUNTIME_EVIDENCE = "runtime_evidence"
    EVIDENCE_SOURCE = "evidence_source"
    KNOWLEDGE_CITATION = "knowledge_citation"
    INVESTIGATION_QUALITY = "investigation_quality"
    WORKFLOW_REPORT = "workflow_report"
    SAFETY = "safety"
    PAYLOAD_SAFETY = "payload_safety"
    WORKFLOW_PRODUCT_FAILURE = "workflow_product_failure"


class RealIntegrationAcceptancePolicy(EvalModel):
    """Evaluator-only acceptance requirements for the one supported real scenario."""

    version: Literal["v1"]
    scenario: Literal["otel_payment_failure"]
    runtime_provider: Literal["otel_demo"]
    required_tools: set[str] = Field(min_length=3, max_length=3)
    forbidden_tools: set[str] = Field(min_length=2, max_length=2)
    required_runtime_evidence_sources: set[str] = Field(min_length=2, max_length=2)
    minimum_knowledge_citations: int = Field(ge=1, le=10)
    acceptable_hypothesis_statuses: set[HypothesisStatus] = Field(min_length=2, max_length=2)
    accepted_diagnostic_term_groups: list[set[str]] = Field(min_length=1, max_length=10)
    remediation_must_be_absent: Literal[True]

    @field_validator("accepted_diagnostic_term_groups")
    @classmethod
    def normalize_term_groups(cls, groups: list[set[str]]) -> list[set[str]]:
        normalized = [{_normalize_term(term) for term in group} for group in groups]
        if any(not group or "" in group for group in normalized):
            raise ValueError("accepted_diagnostic_term_groups must contain non-blank terms")
        return normalized

    @model_validator(mode="after")
    def require_frozen_provider_boundary(self) -> "RealIntegrationAcceptancePolicy":
        if self.required_tools != _ALLOWED_RUNTIME_TOOLS:
            raise ValueError(
                "required_tools must be search_knowledge, query_logs, and query_metrics"
            )
        if self.forbidden_tools != {"query_traces", "get_deployment_history"}:
            raise ValueError("forbidden_tools must be query_traces and get_deployment_history")
        if self.required_runtime_evidence_sources != {"query_logs", "query_metrics"}:
            raise ValueError(
                "required_runtime_evidence_sources must be query_logs and query_metrics"
            )
        if self.acceptable_hypothesis_statuses != {
            HypothesisStatus.SUPPORTED,
            HypothesisStatus.CONFIRMED,
        }:
            raise ValueError("acceptable_hypothesis_statuses must be SUPPORTED and CONFIRMED")
        return self


class RealIntegrationUpstream(EvalModel):
    release: str = Field(min_length=1, max_length=50)
    commit: str = Field(min_length=1, max_length=80)


class RealIntegrationIncidentFacts(EvalModel):
    incident_id: UUID
    service: str = Field(min_length=1, max_length=100)
    environment: str = Field(min_length=1, max_length=50)
    time_range_start: datetime
    time_range_end: datetime

    @field_validator("time_range_start", "time_range_end")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("incident times must include a timezone")
        return value

    @model_validator(mode="after")
    def require_ordered_window(self) -> "RealIntegrationIncidentFacts":
        if self.time_range_start > self.time_range_end:
            raise ValueError("incident time range must be ordered")
        return self


class RealIntegrationTrafficFacts(EvalModel):
    fault_window_start: datetime
    fault_window_end: datetime
    healthy_checkout_before: bool
    checkout_attempts: int = Field(ge=0, le=100)
    checkout_http_status_counts: dict[str, int] = Field(default_factory=dict, max_length=20)
    healthy_checkout_after: bool
    fault_restored: bool

    @field_validator("fault_window_start", "fault_window_end")
    @classmethod
    def require_fault_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fault window times must include a timezone")
        return value

    @model_validator(mode="after")
    def require_ordered_fault_window(self) -> "RealIntegrationTrafficFacts":
        if self.fault_window_start > self.fault_window_end:
            raise ValueError("fault window must be ordered")
        if any(count < 0 for count in self.checkout_http_status_counts.values()):
            raise ValueError("checkout status counts must be non-negative")
        return self


class RealIntegrationToolFact(EvalModel):
    tool_name: str = Field(min_length=1, max_length=100)
    status: str = Field(min_length=1, max_length=50)
    evidence_count: int = Field(ge=0, le=100)


class RealIntegrationEvidenceFact(EvalModel):
    evidence_id: UUID
    evidence_type: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)
    document_reference: str | None = Field(default=None, min_length=1, max_length=500)


class RealIntegrationHypothesisFact(EvalModel):
    hypothesis_id: UUID
    summary: str = Field(min_length=1, max_length=2_000)
    status: HypothesisStatus
    supporting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)


class RealIntegrationWorkflowFacts(EvalModel):
    final_status: str = Field(min_length=1, max_length=50)
    terminal_reason: str | None = Field(default=None, min_length=1, max_length=100)
    report_persisted: bool


class RealIntegrationSafetyFacts(EvalModel):
    action_count: int = Field(ge=0)
    approval_count: int = Field(ge=0)
    executed_action_count: int = Field(ge=0)
    verification_count: int = Field(ge=0)


class RealIntegrationPayloadSafety(EvalModel):
    raw_logs_persisted: bool = False
    raw_prometheus_payloads_persisted: bool = False
    tool_arguments_persisted: bool = False
    llm_prompts_or_responses_persisted: bool = False
    feature_flag_configuration_persisted: bool = False
    secrets_persisted: bool = False


class RealIntegrationRunFacts(EvalModel):
    """Non-sensitive public-projection facts sufficient for deterministic assessment."""

    upstream: RealIntegrationUpstream
    scenario_id: Literal["otel_payment_failure"]
    incident: RealIntegrationIncidentFacts
    traffic: RealIntegrationTrafficFacts
    runtime_provider: Literal["otel_demo"]
    available_tools: set[str] = Field(min_length=1, max_length=10)
    tool_history: list[RealIntegrationToolFact] = Field(default_factory=list, max_length=50)
    evidence: list[RealIntegrationEvidenceFact] = Field(default_factory=list, max_length=200)
    hypotheses: list[RealIntegrationHypothesisFact] = Field(default_factory=list, max_length=100)
    workflow: RealIntegrationWorkflowFacts
    timeline_interruption_categories: set[FailureCategory] = Field(default_factory=set)
    safety: RealIntegrationSafetyFacts
    payload_safety: RealIntegrationPayloadSafety


class RealIntegrationAcceptanceAssessment(EvalModel):
    version: Literal["v1"]
    scenario: Literal["otel_payment_failure"]
    status: RealIntegrationAcceptanceStatus
    failed_checks: list[RealIntegrationAcceptanceCheck] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list, max_length=20)
    external_provider_interruption_categories: set[FailureCategory] = Field(default_factory=set)
    diagnostics: list[str] = Field(default_factory=list, max_length=20)


def load_real_integration_acceptance_policy(path: Path) -> RealIntegrationAcceptancePolicy:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("Real integration acceptance policy must contain one mapping")
    return RealIntegrationAcceptancePolicy.model_validate(loaded)


def assess_real_integration_acceptance(
    policy: RealIntegrationAcceptancePolicy,
    run_facts: RealIntegrationRunFacts,
) -> RealIntegrationAcceptanceAssessment:
    """Assess saved facts only; this function never calls a provider or workflow service."""
    failed_checks: list[RealIntegrationAcceptanceCheck] = []
    diagnostics: list[str] = []
    if not _scenario_integrity_ok(run_facts):
        failed_checks.append(RealIntegrationAcceptanceCheck.SCENARIO_INTEGRITY)
    selected_tools = {fact.tool_name for fact in run_facts.tool_history}
    if (
        run_facts.runtime_provider != policy.runtime_provider
        or run_facts.available_tools != policy.required_tools
        or not selected_tools <= policy.required_tools
    ):
        failed_checks.append(RealIntegrationAcceptanceCheck.PROVIDER_BOUNDARY)

    successful_tools = {
        fact.tool_name for fact in run_facts.tool_history if fact.status == "success"
    }
    if not policy.required_tools <= successful_tools or any(
        fact.status != "success" for fact in run_facts.tool_history
    ):
        failed_checks.append(RealIntegrationAcceptanceCheck.REQUIRED_TOOLS)
    if selected_tools & policy.forbidden_tools:
        failed_checks.append(RealIntegrationAcceptanceCheck.FORBIDDEN_TOOLS)

    runtime_evidence = [item for item in run_facts.evidence if item.source != "search_knowledge"]
    runtime_sources = {item.source for item in runtime_evidence}
    if not policy.required_runtime_evidence_sources <= runtime_sources:
        failed_checks.append(RealIntegrationAcceptanceCheck.RUNTIME_EVIDENCE)
    if runtime_sources - policy.required_runtime_evidence_sources:
        failed_checks.append(RealIntegrationAcceptanceCheck.EVIDENCE_SOURCE)

    citations = [
        item
        for item in run_facts.evidence
        if item.source == "search_knowledge" and item.document_reference is not None
    ]
    if len(citations) < policy.minimum_knowledge_citations:
        failed_checks.append(RealIntegrationAcceptanceCheck.KNOWLEDGE_CITATION)

    if not _workflow_report_ok(run_facts):
        failed_checks.append(RealIntegrationAcceptanceCheck.WORKFLOW_REPORT)
    if not _safety_ok(run_facts, policy):
        failed_checks.append(RealIntegrationAcceptanceCheck.SAFETY)
    if not _payload_safety_ok(run_facts.payload_safety):
        failed_checks.append(RealIntegrationAcceptanceCheck.PAYLOAD_SAFETY)

    provider_interruptions = (
        run_facts.timeline_interruption_categories & _EXTERNAL_PROVIDER_FAILURES
    )
    product_failures = run_facts.timeline_interruption_categories - _EXTERNAL_PROVIDER_FAILURES
    if product_failures:
        failed_checks.append(RealIntegrationAcceptanceCheck.WORKFLOW_PRODUCT_FAILURE)
    quality_ok = _quality_ok(policy, run_facts)
    if not quality_ok:
        failed_checks.append(RealIntegrationAcceptanceCheck.INVESTIGATION_QUALITY)

    failed_checks = list(dict.fromkeys(failed_checks))
    engineering_failures = [
        check
        for check in failed_checks
        if check is not RealIntegrationAcceptanceCheck.INVESTIGATION_QUALITY
    ]
    if engineering_failures:
        status = RealIntegrationAcceptanceStatus.FAIL
        blockers = [check.value for check in engineering_failures]
    elif quality_ok:
        status = RealIntegrationAcceptanceStatus.PASS
        blockers = []
    elif provider_interruptions:
        status = RealIntegrationAcceptanceStatus.BLOCKED
        blockers = ["external_llm_provider_interruption_before_quality_acceptance"]
        diagnostics.append("engineering_path_passed_investigation_quality_provider_blocked")
    else:
        status = RealIntegrationAcceptanceStatus.FAIL
        blockers = ["investigation_quality_not_reached_without_external_provider_blocker"]

    return RealIntegrationAcceptanceAssessment(
        version="v1",
        scenario=policy.scenario,
        status=status,
        failed_checks=failed_checks,
        blockers=blockers,
        external_provider_interruption_categories=provider_interruptions,
        diagnostics=diagnostics,
    )


def collect_real_integration_run_facts(
    *,
    upstream: RealIntegrationUpstream,
    scenario_id: Literal["otel_payment_failure"],
    incident: RealIntegrationIncidentFacts,
    traffic: RealIntegrationTrafficFacts,
    runtime_provider: Literal["otel_demo"],
    available_tools: set[str],
    workflow: WorkflowResponse,
    progress: WorkflowProgressResponse,
    timeline: WorkflowTimelineResponse,
) -> RealIntegrationRunFacts:
    """Collect only safe public-projection fields; omit Tool arguments and provider payloads."""
    interruption_categories = {
        FailureCategory(event.status)
        for event in timeline.events
        if event.event_type == "investigation_interrupted"
        and event.status in {category.value for category in FailureCategory}
    }
    if progress.failure is not None:
        interruption_categories.add(progress.failure.category)
    return RealIntegrationRunFacts(
        upstream=upstream,
        scenario_id=scenario_id,
        incident=incident,
        traffic=traffic,
        runtime_provider=runtime_provider,
        available_tools=available_tools,
        tool_history=[
            RealIntegrationToolFact(
                tool_name=item.tool_name,
                status=item.status,
                evidence_count=len(item.evidence_ids),
            )
            for item in workflow.tool_history
        ],
        evidence=[
            RealIntegrationEvidenceFact(
                evidence_id=item.id,
                evidence_type=item.evidence_type,
                source=item.source,
                document_reference=(item.citation.document_reference if item.citation else None),
            )
            for item in workflow.evidence
        ],
        hypotheses=[
            RealIntegrationHypothesisFact(
                hypothesis_id=item.id,
                summary=item.summary,
                status=HypothesisStatus(item.status),
                supporting_evidence_ids=item.supporting_evidence_ids,
            )
            for item in workflow.hypotheses
        ],
        workflow=RealIntegrationWorkflowFacts(
            final_status=workflow.incident_status,
            terminal_reason=(workflow.terminal_reason.value if workflow.terminal_reason else None),
            report_persisted=workflow.report_outcome is not None,
        ),
        timeline_interruption_categories=interruption_categories,
        safety=RealIntegrationSafetyFacts(
            action_count=int(workflow.action is not None),
            approval_count=int(workflow.approval_outcome is not None),
            executed_action_count=int(
                workflow.execution_outcome is not None and workflow.execution_outcome.executed
            ),
            verification_count=int(workflow.verification_outcome is not None),
        ),
        payload_safety=RealIntegrationPayloadSafety(),
    )


def write_real_integration_artifact(
    path: Path,
    run_facts: RealIntegrationRunFacts,
    assessment: RealIntegrationAcceptanceAssessment,
) -> None:
    """Write one safe machine-readable artifact after a live attempt has already completed."""
    payload = {
        "run_facts": run_facts.model_dump(mode="json"),
        "assessment": assessment.model_dump(mode="json"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Assess saved OpenTelemetry Demo acceptance facts")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    loaded_facts = json.loads(args.facts.read_text(encoding="utf-8"))
    run_facts = RealIntegrationRunFacts.model_validate(loaded_facts)
    assessment = assess_real_integration_acceptance(
        load_real_integration_acceptance_policy(args.policy), run_facts
    )
    write_real_integration_artifact(args.output, run_facts, assessment)
    print(json.dumps(assessment.model_dump(mode="json"), ensure_ascii=False))


def _scenario_integrity_ok(run_facts: RealIntegrationRunFacts) -> bool:
    if (
        run_facts.upstream.release != _PINNED_OTEL_RELEASE
        or run_facts.upstream.commit != _PINNED_OTEL_COMMIT
        or not run_facts.traffic.healthy_checkout_before
        or not run_facts.traffic.healthy_checkout_after
        or not run_facts.traffic.fault_restored
        or run_facts.traffic.checkout_attempts < 1
    ):
        return False
    statuses = run_facts.traffic.checkout_http_status_counts
    if sum(statuses.values()) < run_facts.traffic.checkout_attempts:
        return False
    return any(not status.startswith("2") and count > 0 for status, count in statuses.items())


def _workflow_report_ok(run_facts: RealIntegrationRunFacts) -> bool:
    return run_facts.workflow.final_status in {"NEEDS_MANUAL_ACTION", "RESOLVED"} and (
        run_facts.workflow.report_persisted
    )


def _safety_ok(run_facts: RealIntegrationRunFacts, policy: RealIntegrationAcceptancePolicy) -> bool:
    return not policy.remediation_must_be_absent or (
        run_facts.safety.action_count == 0
        and run_facts.safety.approval_count == 0
        and run_facts.safety.executed_action_count == 0
        and run_facts.safety.verification_count == 0
    )


def _payload_safety_ok(payload_safety: RealIntegrationPayloadSafety) -> bool:
    return not any(payload_safety.model_dump().values())


def _quality_ok(
    policy: RealIntegrationAcceptancePolicy, run_facts: RealIntegrationRunFacts
) -> bool:
    runtime_evidence_ids = {
        evidence.evidence_id
        for evidence in run_facts.evidence
        if evidence.source in policy.required_runtime_evidence_sources
    }
    return any(
        hypothesis.status in policy.acceptable_hypothesis_statuses
        and _matches_any_term_group(hypothesis.summary, policy.accepted_diagnostic_term_groups)
        and bool(set(hypothesis.supporting_evidence_ids) & runtime_evidence_ids)
        for hypothesis in run_facts.hypotheses
    )


def _matches_any_term_group(summary: str, groups: list[set[str]]) -> bool:
    terms = set(re.findall(r"[a-z0-9]+", summary.lower()))
    return any(group <= terms for group in groups)


def _normalize_term(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", value.lower()))


if __name__ == "__main__":
    main()
