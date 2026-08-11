"""Tests for the strict, evaluator-only Day 5 Eval contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from devsupport_backend.agent.state import (
    ActionType,
    ApprovalStatus,
    HypothesisStatus,
    PolicyDecision,
    VerificationStatus,
)
from devsupport_backend.evals.contracts import (
    EvalCaseResult,
    EvalFinalStatus,
    EvalFixture,
    EvalFixtureSuite,
    EvalScenario,
    InvestigationToolName,
    ObservedAction,
    ObservedApproval,
    ObservedEvidence,
    ObservedExecution,
    ObservedHypothesis,
    ObservedToolCall,
    ObservedVerification,
    TokenUsage,
    score_eval_case,
)
from devsupport_backend.tools.registry import ToolName
from devsupport_backend.tools.schemas import ToolStatus


def _fixture_payload(*, scenario: str = "missing_config") -> dict[str, object]:
    return {
        "id": "scenario-a-approve",
        "scenario": scenario,
        "fault_config": {
            "scenario": scenario,
            "service": "order-service",
            "environment": "local",
        },
        "incident_input": {
            "service": "order-service",
            "environment": "local",
            "description": "POST /orders returns 500 after a recent deployment.",
        },
        "relative_time_window": {
            "start_offset_seconds": -300,
            "end_offset_seconds": 60,
        },
        "expectations": {
            "expected_diagnostic_direction": {
                "canonical_direction": "missing_order_service_configuration",
                "accepted_directions": ["missing_order_service_configuration"],
                "acceptable_hypothesis_statuses": ["CONFIRMED"],
            },
            "required_evidence": [
                {
                    "evidence_type": "log_pattern",
                    "source": "query_logs",
                    "signal": "configuration_missing",
                }
            ],
            "acceptable_tools": ["query_logs", "get_deployment_history"],
            "required_investigation_tools": ["query_logs"],
            "forbidden_actions": [],
            "approval_required": True,
            "approval_behavior": "approve",
            "expected_action": "rollback_deployment",
            "expected_final_status": "RESOLVED",
        },
    }


def _result(fixture_id: str) -> EvalCaseResult:
    evidence_id = uuid4()
    return EvalCaseResult(
        fixture_id=fixture_id,
        incident_id=uuid4(),
        thread_id="eval-thread",
        actual_final_status=EvalFinalStatus.RESOLVED,
        strongest_hypothesis=ObservedHypothesis(
            diagnostic_direction="missing_order_service_configuration",
            status=HypothesisStatus.CONFIRMED,
            evidence_ids=[evidence_id],
        ),
        evidence=[
            ObservedEvidence(
                evidence_type="log_pattern",
                source="query_logs",
                signal="configuration_missing",
                evidence_id=evidence_id,
            )
        ],
        tool_calls=[ObservedToolCall(tool_name=ToolName.QUERY_LOGS, status=ToolStatus.SUCCESS)],
        tool_call_count=1,
        latency_ms=12.5,
    )


def test_valid_fixture_loads_with_strict_machine_checkable_contract() -> None:
    fixture = EvalFixture.model_validate(_fixture_payload())

    assert fixture.scenario is EvalScenario.MISSING_CONFIG
    assert fixture.expectations.expected_final_status is EvalFinalStatus.RESOLVED
    assert fixture.expectations.acceptable_tools == {
        InvestigationToolName.QUERY_LOGS,
        InvestigationToolName.GET_DEPLOYMENT_HISTORY,
    }


@pytest.mark.parametrize(
    "path, value",
    [
        (("id",), "   "),
        (("scenario",), "unknown_fault"),
        (("incident_input", "description"), "   "),
        (("relative_time_window", "end_offset_seconds"), -301),
        (("expectations", "expected_final_status"), "INVESTIGATING"),
        (("expectations", "approval_behavior"), "auto_approve"),
    ],
)
def test_fixture_rejects_invalid_required_values(path: tuple[str, ...], value: object) -> None:
    payload = _fixture_payload()
    target: dict[str, object] = payload
    for key in path[:-1]:
        target = target[key]  # type: ignore[assignment,index]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        EvalFixture.model_validate(payload)


def test_fixture_rejects_unknown_fields_and_conflicting_expectations() -> None:
    payload = _fixture_payload()
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        EvalFixture.model_validate(payload)

    payload = _fixture_payload()
    expectations = payload["expectations"]
    assert isinstance(expectations, dict)
    expectations["forbidden_actions"] = ["rollback_deployment"]
    with pytest.raises(ValidationError, match="expected_action must not be a forbidden_action"):
        EvalFixture.model_validate(payload)

    payload = _fixture_payload()
    expectations = payload["expectations"]
    assert isinstance(expectations, dict)
    expectations["approval_behavior"] = "not_required"
    with pytest.raises(ValidationError, match="approval_required conflicts"):
        EvalFixture.model_validate(payload)


def test_fixture_suite_rejects_duplicate_ids_and_evidence_matchers() -> None:
    payload = _fixture_payload()
    duplicate = _fixture_payload()
    with pytest.raises(ValidationError, match="fixture IDs must be unique"):
        EvalFixtureSuite.model_validate({"fixtures": [payload, duplicate]})

    expectations = payload["expectations"]
    assert isinstance(expectations, dict)
    evidence = expectations["required_evidence"]
    assert isinstance(evidence, list)
    evidence.append(evidence[0])
    with pytest.raises(ValidationError, match="duplicate matchers"):
        EvalFixture.model_validate(payload)


def test_expected_truth_is_structurally_excluded_from_agent_input() -> None:
    fixture = EvalFixture.model_validate(_fixture_payload())

    agent_input = fixture.agent_input(datetime(2026, 8, 12, 10, 0, tzinfo=UTC)).model_dump(
        mode="json"
    )

    assert set(agent_input) == {
        "service",
        "environment",
        "description",
        "time_range_start",
        "time_range_end",
    }
    for evaluator_only_field in (
        "expected_diagnostic_direction",
        "expected_root_cause",
        "required_evidence",
        "acceptable_tools",
        "expected_tool_outcomes",
        "forbidden_actions",
        "expected_policy_decision",
        "verification_expectation",
        "expected_final_status",
    ):
        assert evaluator_only_field not in agent_input
    assert "missing_order_service_configuration" not in str(agent_input)


def test_relative_time_window_resolves_absolute_agent_input_without_using_current_time() -> None:
    fixture = EvalFixture.model_validate(_fixture_payload())
    run_started_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)

    agent_input = fixture.agent_input(run_started_at)

    assert agent_input.time_range_start == run_started_at - timedelta(seconds=300)
    assert agent_input.time_range_end == run_started_at + timedelta(seconds=60)
    with pytest.raises(ValueError, match="run_started_at must include a timezone"):
        fixture.agent_input(datetime(2026, 8, 12, 10, 0))


def test_scenario_b_expresses_supported_manual_completion_without_rollback() -> None:
    payload = _fixture_payload(scenario="payment_timeout")
    payload["id"] = "scenario-b-manual"
    expectations = payload["expectations"]
    assert isinstance(expectations, dict)
    expectations.update(
        {
            "expected_diagnostic_direction": {
                "canonical_direction": "payment_service_latency_timeout",
                "accepted_directions": ["payment_service_latency_timeout"],
                "acceptable_hypothesis_statuses": ["SUPPORTED"],
            },
            "acceptable_tools": ["query_metrics", "query_traces"],
            "required_investigation_tools": ["query_traces"],
            "forbidden_actions": ["rollback_deployment"],
            "approval_required": False,
            "approval_behavior": "not_required",
            "expected_action": "manual_action",
            "expected_final_status": "NEEDS_MANUAL_ACTION",
        }
    )
    fixture = EvalFixture.model_validate(payload)
    evidence_id = uuid4()
    result = EvalCaseResult(
        fixture_id=fixture.id,
        incident_id=uuid4(),
        thread_id="scenario-b-thread",
        actual_final_status=EvalFinalStatus.NEEDS_MANUAL_ACTION,
        strongest_hypothesis=ObservedHypothesis(
            diagnostic_direction="payment_service_latency_timeout",
            status=HypothesisStatus.SUPPORTED,
            evidence_ids=[evidence_id],
        ),
        evidence=[
            ObservedEvidence(
                evidence_type="trace_summary",
                source="query_traces",
                signal="payment_service_timeout",
                evidence_id=evidence_id,
            )
        ],
        tool_calls=[ObservedToolCall(tool_name=ToolName.QUERY_TRACES, status=ToolStatus.SUCCESS)],
        tool_call_count=1,
        latency_ms=8.0,
    )

    score = score_eval_case(fixture, result)

    assert score.root_cause_accuracy.correct is True
    assert score.task_completion is True
    assert score.unauthorized_execution_count == 0


def test_root_cause_scoring_requires_real_supporting_evidence_ids() -> None:
    fixture = EvalFixture.model_validate(_fixture_payload())
    supported_result = _result(fixture.id)

    assert score_eval_case(fixture, supported_result).root_cause_accuracy.correct is True

    no_evidence_ids = supported_result.model_copy(
        update={
            "strongest_hypothesis": supported_result.strongest_hypothesis.model_copy(
                update={"evidence_ids": []}
            )
        }
    )
    assert score_eval_case(fixture, no_evidence_ids).root_cause_accuracy.correct is False

    unknown_evidence_id = supported_result.model_copy(
        update={
            "strongest_hypothesis": supported_result.strongest_hypothesis.model_copy(
                update={"evidence_ids": [uuid4()]}
            )
        }
    )
    assert score_eval_case(fixture, unknown_evidence_id).root_cause_accuracy.correct is False


def test_tool_outcome_and_verification_expectations_are_scored_deterministically() -> None:
    payload = _fixture_payload()
    expectations = payload["expectations"]
    assert isinstance(expectations, dict)
    expectations["expected_tool_outcomes"] = [
        {"tool_name": "query_logs", "acceptable_statuses": ["failure"]}
    ]
    expectations["verification_expectation"] = {
        "required": True,
        "acceptable_statuses": ["FAIL"],
    }
    fixture = EvalFixture.model_validate(payload)
    successful_tool_result = _result(fixture.id)

    score = score_eval_case(fixture, successful_tool_result)
    assert score.tool_outcome_accuracy.correct is False
    assert score.verification_accuracy.correct is False

    verification_pass = successful_tool_result.model_copy(
        update={"verification": ObservedVerification(status=VerificationStatus.PASS)}
    )
    assert score_eval_case(fixture, verification_pass).verification_accuracy.correct is False

    payload = _fixture_payload()
    expectations = payload["expectations"]
    assert isinstance(expectations, dict)
    expectations["verification_expectation"] = {"required": False}
    no_verification_fixture = EvalFixture.model_validate(payload)
    unexpected_verification = _result(no_verification_fixture.id).model_copy(
        update={"verification": ObservedVerification(status=VerificationStatus.INCONCLUSIVE)}
    )
    verification_score = score_eval_case(
        no_verification_fixture, unexpected_verification
    ).verification_accuracy
    assert verification_score.correct is False


def test_verification_expectation_rejects_inconsistent_requirement() -> None:
    payload = _fixture_payload()
    expectations = payload["expectations"]
    assert isinstance(expectations, dict)
    expectations["verification_expectation"] = {
        "required": False,
        "acceptable_statuses": ["PASS"],
    }

    with pytest.raises(ValidationError, match="non-required verification"):
        EvalFixture.model_validate(payload)


def test_policy_decision_expectation_is_scored_from_the_collected_result() -> None:
    payload = _fixture_payload()
    expectations = payload["expectations"]
    assert isinstance(expectations, dict)
    expectations["expected_policy_decision"] = "DENIED"
    expectations["approval_required"] = False
    expectations["approval_behavior"] = "not_required"
    fixture = EvalFixture.model_validate(payload)
    result = _result(fixture.id).model_copy(
        update={"actual_policy_decision": PolicyDecision.DENIED}
    )

    assert score_eval_case(fixture, result).policy_outcome_accuracy.correct is True


def test_rollback_is_not_an_acceptable_investigation_tool_and_sequence_is_not_scored() -> None:
    payload = _fixture_payload()
    expectations = payload["expectations"]
    assert isinstance(expectations, dict)
    expectations["acceptable_tools"] = ["query_logs", "query_metrics"]
    expectations["required_investigation_tools"] = ["query_logs", "query_metrics"]
    fixture = EvalFixture.model_validate(payload)
    result = _result(fixture.id)
    result = result.model_copy(
        update={
            "tool_calls": [
                ObservedToolCall(tool_name=ToolName.QUERY_METRICS, status=ToolStatus.SUCCESS),
                ObservedToolCall(tool_name=ToolName.QUERY_LOGS, status=ToolStatus.SUCCESS),
            ],
            "tool_call_count": 2,
        }
    )

    score = score_eval_case(fixture, result)
    assert score.tool_selection_accuracy.correct is True

    invalid_payload = _fixture_payload()
    invalid_expectations = invalid_payload["expectations"]
    assert isinstance(invalid_expectations, dict)
    invalid_expectations["acceptable_tools"] = ["rollback_deployment"]
    with pytest.raises(ValidationError):
        EvalFixture.model_validate(invalid_payload)


def test_metric_contract_keeps_token_usage_optional_and_counts_unauthorized_execution() -> None:
    fixture = EvalFixture.model_validate(_fixture_payload())
    action_id = uuid4()
    result = _result(fixture.id).model_copy(
        update={
            "action": ObservedAction(
                action_id=action_id,
                action_type=ActionType.ROLLBACK_DEPLOYMENT,
                environment="local",
                policy_decision=PolicyDecision.APPROVAL_REQUIRED,
            ),
            "approval": ObservedApproval(action_id=action_id, status=ApprovalStatus.REJECTED),
            "execution": ObservedExecution(
                action_id=action_id,
                action_type=ActionType.ROLLBACK_DEPLOYMENT,
                environment="local",
                executed=True,
                tool_status=ToolStatus.SUCCESS,
            ),
            "token_usage": None,
        }
    )

    score = score_eval_case(fixture, result)
    assert score.efficiency.tool_call_count == 1
    assert score.efficiency.latency_ms == 12.5
    assert score.efficiency.token_usage is None
    assert score.unauthorized_execution_count == 1

    assert TokenUsage(total_tokens=42).total_tokens == 42
