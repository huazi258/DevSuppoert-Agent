"""Validation and coverage tests for the fixed initial V0 Eval Suite."""

from pathlib import Path

from devsupport_backend.agent.state import (
    ActionType,
    HypothesisStatus,
    PolicyDecision,
    VerificationStatus,
)
from devsupport_backend.evals.contracts import (
    ApprovalBehavior,
    EvalFinalStatus,
    EvalScenario,
    load_eval_fixture_suite,
)
from devsupport_backend.tools.schemas import ToolStatus

SUITE_PATH = Path(__file__).resolve().parents[3] / "evals" / "initial_suite.yaml"


def test_initial_suite_strictly_loads_with_eight_unique_cases() -> None:
    suite = load_eval_fixture_suite(SUITE_PATH)

    assert len(suite.fixtures) >= 8
    assert len({fixture.id for fixture in suite.fixtures}) == len(suite.fixtures)


def test_initial_suite_covers_required_day_five_cases() -> None:
    fixtures = {fixture.id: fixture for fixture in load_eval_fixture_suite(SUITE_PATH).fixtures}

    assert {
        "a-approve-happy",
        "a-wording-variant",
        "a-approval-reject",
        "a-production-policy-denied",
        "b-payment-timeout-standard",
        "b-wording-noise-variant",
        "a-query-logs-tool-failure",
        "a-recovery-verification-failure",
    } <= fixtures.keys()
    assert fixtures["a-approve-happy"].scenario is EvalScenario.MISSING_CONFIG
    assert fixtures["b-payment-timeout-standard"].scenario is EvalScenario.PAYMENT_TIMEOUT
    assert fixtures["a-production-policy-denied"].expectations.expected_policy_decision is (
        PolicyDecision.DENIED
    )


def test_wording_variants_are_distinct_symptom_only_incident_descriptions() -> None:
    fixtures = {fixture.id: fixture for fixture in load_eval_fixture_suite(SUITE_PATH).fixtures}

    assert (
        fixtures["a-approve-happy"].incident_input.description
        != fixtures["a-wording-variant"].incident_input.description
    )
    assert (
        fixtures["b-payment-timeout-standard"].incident_input.description
        != fixtures["b-wording-noise-variant"].incident_input.description
    )


def test_safety_and_failure_path_expectations_are_explicit() -> None:
    fixtures = {fixture.id: fixture for fixture in load_eval_fixture_suite(SUITE_PATH).fixtures}
    scenario_b = [
        fixture for fixture in fixtures.values() if fixture.scenario is EvalScenario.PAYMENT_TIMEOUT
    ]

    assert all(
        ActionType.ROLLBACK_DEPLOYMENT in fixture.expectations.forbidden_actions
        for fixture in scenario_b
    )
    assert all(
        fixture.expectations.expected_diagnostic_direction.acceptable_hypothesis_statuses
        == {HypothesisStatus.SUPPORTED}
        and fixture.expectations.expected_action is ActionType.MANUAL_ACTION
        and fixture.expectations.expected_final_status is EvalFinalStatus.NEEDS_MANUAL_ACTION
        for fixture in scenario_b
    )
    assert fixtures["a-approval-reject"].expectations.approval_behavior is ApprovalBehavior.REJECT
    assert all(
        fixture.expectations.verification_expectation is not None
        and fixture.expectations.verification_expectation.required is False
        for fixture in (
            *scenario_b,
            fixtures["a-approval-reject"],
            fixtures["a-production-policy-denied"],
        )
    )
    assert all(
        fixture.expectations.approval_behavior is ApprovalBehavior.NOT_REQUIRED
        for fixture in (*scenario_b, fixtures["a-production-policy-denied"])
    )
    tool_failure = fixtures["a-query-logs-tool-failure"].expectations.expected_tool_outcomes
    assert len(tool_failure) == 1
    assert tool_failure[0].tool_name.value == "query_logs"
    assert tool_failure[0].acceptable_statuses == {ToolStatus.FAILURE}
    verification_failure = fixtures["a-recovery-verification-failure"].expectations
    assert verification_failure.verification_expectation is not None
    assert verification_failure.verification_expectation.acceptable_statuses == {
        VerificationStatus.FAIL,
        VerificationStatus.INCONCLUSIVE,
    }
    assert verification_failure.expected_final_status is EvalFinalStatus.NEEDS_MANUAL_ACTION


def test_each_fixture_keeps_evaluator_truth_out_of_agent_input_and_description() -> None:
    for fixture in load_eval_fixture_suite(SUITE_PATH).fixtures:
        agent_input = fixture.agent_input().model_dump(mode="json")
        expectations = fixture.expectations

        assert set(agent_input) == {
            "service",
            "environment",
            "description",
            "time_range_start",
            "time_range_end",
        }
        description = fixture.incident_input.description.lower()
        assert expectations.expected_diagnostic_direction.canonical_direction not in description
        assert expectations.expected_final_status.value.lower() not in description
        assert expectations.approval_behavior.value not in description
        if expectations.expected_action is not None:
            assert expectations.expected_action.value not in description
        if expectations.expected_policy_decision is not None:
            assert expectations.expected_policy_decision.value.lower() not in description
        assert all(tool.value not in description for tool in expectations.acceptable_tools)
        assert all(action.value not in description for action in expectations.forbidden_actions)
        assert all(
            evidence.signal not in description for evidence in expectations.required_evidence
        )
