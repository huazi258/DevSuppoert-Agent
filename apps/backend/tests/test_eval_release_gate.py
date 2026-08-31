"""Deterministic V1 release-gate assessment tests without Fault Lab or LLM calls."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from devsupport_backend.evals.contracts import (
    ApprovalTriggerScore,
    EfficiencyMetrics,
    EvalCaseResult,
    EvalExecutionScope,
    EvalReleaseCaseClassification,
    EvalReleaseFailedCheck,
    EvalReleaseGatePolicy,
    EvalReleaseGateStatus,
    EvalScore,
    EvidenceRecallScore,
    InvestigationObservability,
    PartialEvalFacts,
    PolicyOutcomeScore,
    RootCauseScore,
    ToolOutcomeScore,
    ToolSelectionScore,
    VerificationScore,
    load_eval_fixture_suite,
    load_eval_release_gate_policy,
    load_eval_release_profiles,
)
from devsupport_backend.evals.release_gate import assess_eval_release_gate
from devsupport_backend.evals.runner import EvalRunOutput

SUITE_PATH = Path(__file__).resolve().parents[3] / "evals" / "initial_suite.yaml"
PROFILE_PATH = Path(__file__).resolve().parents[3] / "evals" / "v1_release_profiles.yaml"
POLICY_PATH = Path(__file__).resolve().parents[3] / "evals" / "v1_release_gate.yaml"


def _release_inputs(profile_name: str = "p0_fault_lab"):
    suite = load_eval_fixture_suite(SUITE_PATH)
    profiles = load_eval_release_profiles(PROFILE_PATH, suite)
    profile = profiles.profiles[profile_name]
    selected = suite.model_copy(
        update={
            "fixtures": [
                fixture for fixture in suite.fixtures if fixture.id in profile.case_ids
            ]
        }
    )
    return profile, selected, load_eval_release_gate_policy(POLICY_PATH)


def _score(
    fixture_id: str,
    *,
    root_cause_correct: bool = True,
    evidence_recall: float = 1.0,
    unauthorized_execution_count: int = 0,
) -> EvalScore:
    return EvalScore(
        fixture_id=fixture_id,
        root_cause_accuracy=RootCauseScore(
            correct=root_cause_correct,
            diagnostic_direction_correct=root_cause_correct,
            grounded_conclusion_correct=root_cause_correct,
        ),
        key_evidence_recall=EvidenceRecallScore(
            covered=2 if evidence_recall == 1 else 1,
            required=2,
            recall=evidence_recall,
        ),
        tool_selection_accuracy=ToolSelectionScore(
            correct=True,
            acceptable_tools_only=True,
            required_tools_covered=True,
            forbidden_action_observed=False,
        ),
        tool_outcome_accuracy=ToolOutcomeScore(applicable=False, correct=None),
        task_completion=True,
        approval_trigger_accuracy=ApprovalTriggerScore(correct=True, approval_created=True),
        policy_outcome_accuracy=PolicyOutcomeScore(applicable=False, correct=None),
        verification_accuracy=VerificationScore(
            applicable=False,
            correct=None,
            verification_observed=False,
        ),
        unauthorized_execution_count=unauthorized_execution_count,
        efficiency=EfficiencyMetrics(tool_call_count=1, latency_ms=1.0, llm_call_count=1),
    )


def _full_output(
    fixture_id: str,
    *,
    passed: bool = True,
    score: EvalScore | None | object = ...,
    failure_category: str | None = None,
    observability: InvestigationObservability | None = None,
    partial_facts: PartialEvalFacts | None = None,
    result: EvalCaseResult | None | object = ...,
) -> EvalRunOutput:
    resolved_score = _score(fixture_id) if score is ... else score
    if result is ...:
        result = EvalCaseResult(
            fixture_id=fixture_id,
            incident_id=uuid4(),
            thread_id=f"{fixture_id}-thread",
            actual_final_status="NEEDS_MANUAL_ACTION",
            tool_call_count=0,
            latency_ms=1.0,
        )
    return EvalRunOutput(
        fixture_id=fixture_id,
        execution_scope=EvalExecutionScope.FULL_WORKFLOW,
        incident_id=uuid4(),
        thread_id=f"{fixture_id}-thread",
        final_outcome="NEEDS_MANUAL_ACTION",
        score=resolved_score,  # type: ignore[arg-type]
        result=result,  # type: ignore[arg-type]
        passed=passed,
        latency_ms=1.0,
        llm_call_count=1,
        partial_facts=partial_facts,
        failure_category=failure_category,
        observability=observability,
    )


def _policy_output(*, passed: bool = True) -> EvalRunOutput:
    return EvalRunOutput(
        fixture_id="production-policy-gate-denied",
        execution_scope=EvalExecutionScope.POLICY_GATE_SAFETY,
        incident_id=uuid4(),
        thread_id=None,
        final_outcome="DENIED" if passed else None,
        score=None,
        result=None,
        passed=passed,
        latency_ms=1.0,
    )


def _passing_outputs() -> list[EvalRunOutput]:
    profile, _, _ = _release_inputs()
    return [
        _policy_output()
        if fixture_id == "production-policy-gate-denied"
        else _full_output(fixture_id)
        for fixture_id in profile.case_ids
    ]


def _assessment(outputs: list[EvalRunOutput]):
    profile, suite, policy = _release_inputs()
    return assess_eval_release_gate("p0_fault_lab", profile, suite, policy, outputs)


def test_all_p0_cases_pass_with_complete_safety_evidence() -> None:
    assessment = _assessment(_passing_outputs())

    assert assessment.status is EvalReleaseGateStatus.PASS
    assert assessment.passed_case_count == 6
    assert assessment.safety_gate.passed is True
    assert assessment.aggregate.task_completion_rate == 1.0
    assert assessment.aggregate.policy_safety_pass_rate == 1.0
    assert assessment.aggregate.unauthorized_execution_count == 0


@pytest.mark.parametrize(
    ("failure_category", "observability", "expected_status", "expected_classification"),
    [
        (
            "LLM_PROVIDER_TIMEOUT",
            None,
            EvalReleaseGateStatus.BLOCKED,
            EvalReleaseCaseClassification.EXTERNAL_PROVIDER_BLOCKED,
        ),
        (
            "LLM_PROVIDER_ERROR",
            None,
            EvalReleaseGateStatus.BLOCKED,
            EvalReleaseCaseClassification.EXTERNAL_PROVIDER_BLOCKED,
        ),
        (
            "STRUCTURED_OUTPUT_INVALID",
            None,
            EvalReleaseGateStatus.FAIL,
            EvalReleaseCaseClassification.PRODUCT_FAILURE,
        ),
        (
            None,
            InvestigationObservability(timeout_classification="workflow_timeout"),
            EvalReleaseGateStatus.FAIL,
            EvalReleaseCaseClassification.PRODUCT_FAILURE,
        ),
        (
            None,
            InvestigationObservability(
                timeout_classification="eval_post_processing_timeout",
                workflow_execution_completed_before_timeout=True,
            ),
            EvalReleaseGateStatus.BLOCKED,
            EvalReleaseCaseClassification.EVAL_INFRASTRUCTURE_BLOCKED,
        ),
    ],
)
def test_failure_categories_and_timeout_facts_get_deterministic_release_classification(
    failure_category: str | None,
    observability: InvestigationObservability | None,
    expected_status: EvalReleaseGateStatus,
    expected_classification: EvalReleaseCaseClassification,
) -> None:
    outputs = _passing_outputs()
    outputs[0] = _full_output(
        "a-approve-happy",
        passed=False,
        score=None,
        failure_category=failure_category,
        observability=observability,
        partial_facts=PartialEvalFacts(tool_call_count=1, unauthorized_execution_count=0),
        result=None,
    )

    assessment = _assessment(outputs)

    assert assessment.status is expected_status
    assert assessment.cases[0].classification is expected_classification


def test_scoreable_semantic_failure_fails_and_reports_only_failed_checks() -> None:
    outputs = _passing_outputs()
    outputs[0] = _full_output(
        "a-approve-happy",
        passed=False,
        score=_score("a-approve-happy", root_cause_correct=False, evidence_recall=0.5),
    )

    assessment = _assessment(outputs)

    assert assessment.status is EvalReleaseGateStatus.FAIL
    assert assessment.cases[0].failed_checks == [
        EvalReleaseFailedCheck.ROOT_CAUSE_ACCURACY,
        EvalReleaseFailedCheck.KEY_EVIDENCE_RECALL,
    ]


def test_known_unauthorized_execution_overrides_provider_blocker() -> None:
    outputs = _passing_outputs()
    outputs[0] = _full_output(
        "a-approve-happy",
        passed=False,
        score=_score("a-approve-happy", unauthorized_execution_count=1),
        failure_category="LLM_PROVIDER_TIMEOUT",
    )

    assessment = _assessment(outputs)

    assert assessment.status is EvalReleaseGateStatus.FAIL
    assert assessment.cases[0].classification is EvalReleaseCaseClassification.PRODUCT_FAILURE
    assert assessment.cases[0].reason == "unauthorized_execution_observed"


def test_policy_safety_failure_is_a_product_failure() -> None:
    outputs = _passing_outputs()
    outputs[-1] = _policy_output(passed=False)

    assessment = _assessment(outputs)

    assert assessment.status is EvalReleaseGateStatus.FAIL
    assert assessment.cases[-1].classification is EvalReleaseCaseClassification.PRODUCT_FAILURE


@pytest.mark.parametrize("missing_metric", ["unauthorized", "tool_call"])
def test_missing_safety_metrics_block_release_without_claiming_zero(missing_metric: str) -> None:
    outputs = _passing_outputs()
    outputs[0] = _full_output(
        "a-approve-happy",
        passed=True,
        score=None if missing_metric == "unauthorized" else _score("a-approve-happy"),
        partial_facts=(
            PartialEvalFacts(tool_call_count=1)
            if missing_metric == "unauthorized"
            else PartialEvalFacts(unauthorized_execution_count=0)
        ),
        result=(None if missing_metric == "tool_call" else ...),
    )

    assessment = _assessment(outputs)

    assert assessment.status is EvalReleaseGateStatus.BLOCKED
    assert assessment.safety_gate.passed is False
    if missing_metric == "unauthorized":
        assert assessment.safety_gate.unauthorized_execution_count is None
        assert assessment.safety_gate.unauthorized_execution_metrics_complete is False
    else:
        assert assessment.safety_gate.tool_call_metrics_complete is False


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extended"])
def test_incomplete_or_mixed_p0_outputs_block_release(mutation: str) -> None:
    outputs = _passing_outputs()
    if mutation == "missing":
        outputs.pop(0)
    elif mutation == "duplicate":
        outputs.append(_full_output("a-approve-happy"))
    else:
        outputs.append(_full_output("a-wording-variant"))

    assessment = _assessment(outputs)

    assert assessment.status is EvalReleaseGateStatus.BLOCKED
    assert assessment.eval_infrastructure_blocked_count >= 1


def test_provider_blocker_keeps_failed_attempt_in_aggregate_denominator() -> None:
    outputs = _passing_outputs()
    outputs[0] = _full_output(
        "a-approve-happy",
        passed=False,
        score=None,
        failure_category="LLM_PROVIDER_TIMEOUT",
        partial_facts=PartialEvalFacts(tool_call_count=1, unauthorized_execution_count=0),
        result=None,
    )

    assessment = _assessment(outputs)

    assert assessment.status is EvalReleaseGateStatus.BLOCKED
    assert assessment.aggregate.full_workflow_case_count == 5
    assert assessment.aggregate.root_cause_accuracy == 0.8
    assert assessment.aggregate.task_completion_rate == 0.8


def test_extended_profile_cannot_produce_a_p0_release_pass() -> None:
    profile, suite, policy = _release_inputs("extended_fault_lab")
    outputs = [_full_output(fixture_id) for fixture_id in profile.case_ids]

    assessment = assess_eval_release_gate("extended_fault_lab", profile, suite, policy, outputs)

    assert assessment.status is EvalReleaseGateStatus.BLOCKED


def test_release_gate_policy_strictly_loads_and_rejects_weakened_requirements() -> None:
    policy = load_eval_release_gate_policy(POLICY_PATH)
    assert policy.profile == "p0_fault_lab"

    payload = policy.model_dump(mode="json")
    payload["requirements"]["all_cases_must_pass"] = False  # type: ignore[index]
    with pytest.raises(ValidationError):
        EvalReleaseGatePolicy.model_validate(payload)

    payload = policy.model_dump(mode="json")
    payload["requirements"]["unauthorized_execution_max"] = 1  # type: ignore[index]
    with pytest.raises(ValidationError):
        EvalReleaseGatePolicy.model_validate(payload)
