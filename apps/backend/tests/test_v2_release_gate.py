"""Unit contracts for the independent deterministic V2 release gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

import devsupport_backend.evals.v2_release as release_module
from devsupport_backend.evals.v2_release import (
    V2CaseClassification,
    V2ReleaseCaseResult,
    V2ReleaseStatus,
    assess_v2_release_gate,
    load_v2_release_gate,
    load_v2_release_suite,
    run_v2_release_suite,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SUITE_PATH = REPOSITORY_ROOT / "evals" / "v2_release_suite.yaml"
GATE_PATH = REPOSITORY_ROOT / "evals" / "v2_release_gate.yaml"


def _inputs():
    suite = load_v2_release_suite(SUITE_PATH)
    return suite, load_v2_release_gate(GATE_PATH)


def _passed_results() -> list[V2ReleaseCaseResult]:
    suite, _ = _inputs()
    return [
        V2ReleaseCaseResult(
            case_id=case.id,
            classification=V2CaseClassification.PASSED,
            passed=True,
            duration_ms=1.0,
            expectations=case.expectations,
        )
        for case in suite.cases
    ]


def test_v2_release_assets_define_exactly_the_eight_required_deterministic_cases() -> None:
    suite, policy = _inputs()

    assert len(suite.cases) == 8
    assert set(policy.expected_case_ids) == {case.id for case in suite.cases}
    assert all(case.deterministic for case in suite.cases)


def test_v2_release_gate_passes_only_with_complete_grounded_safe_case_evidence() -> None:
    suite, policy = _inputs()

    assessment = assess_v2_release_gate(suite, policy, _passed_results())

    assert assessment.status is V2ReleaseStatus.PASS
    assert assessment.expected_case_count == assessment.observed_case_count == 8
    assert assessment.passed_case_count == 8
    assert assessment.product_failure_cases == []
    assert assessment.external_provider_blocked_cases == []
    assert assessment.eval_infrastructure_blocked_cases == []
    assert assessment.metrics.terminal_status_correctness is True
    assert assessment.metrics.terminal_reason_complete is True
    assert assessment.metrics.evidence_grounding_correct is True
    assert assessment.metrics.citation_completeness is True
    assert assessment.metrics.scope_isolation is True
    assert assessment.metrics.budget_compliance is True
    assert assessment.metrics.fake_evidence_count == 0
    assert assessment.metrics.side_effect_tool_count == 0
    assert assessment.metrics.scope_leakage_count == 0


def test_v2_release_gate_distinguishes_product_failure_from_infrastructure_blocker() -> None:
    suite, policy = _inputs()
    failed = _passed_results()
    failed[0] = failed[0].model_copy(
        update={
            "classification": V2CaseClassification.PRODUCT_FAILURE,
            "passed": False,
            "failure_reason": "deterministic_contract_failed",
        }
    )

    product_assessment = assess_v2_release_gate(suite, policy, failed)
    assert product_assessment.status is V2ReleaseStatus.FAIL
    assert product_assessment.product_failure_cases == ["successful_investigation"]
    assert product_assessment.metrics.fake_evidence_count is None

    blocked = _passed_results()
    blocked[0] = blocked[0].model_copy(
        update={
            "classification": V2CaseClassification.EVAL_INFRASTRUCTURE_BLOCKED,
            "passed": False,
            "failure_reason": "deterministic_case_timeout",
        }
    )
    blocked_assessment = assess_v2_release_gate(suite, policy, blocked)
    assert blocked_assessment.status is V2ReleaseStatus.BLOCKED
    assert blocked_assessment.eval_infrastructure_blocked_cases == ["successful_investigation"]


def test_v2_release_runner_classifies_pytest_infrastructure_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite, _ = _inputs()

    monkeypatch.setattr(
        release_module.subprocess,
        "run",
        lambda *_, **__: subprocess.CompletedProcess(
            args=[], returncode=5, stdout="no tests ran", stderr=""
        ),
    )

    results = run_v2_release_suite(suite, backend_directory=Path.cwd())

    assert all(
        result.classification is V2CaseClassification.EVAL_INFRASTRUCTURE_BLOCKED
        for result in results
    )


def test_v2_release_suite_rejects_missing_required_core_case() -> None:
    suite, _ = _inputs()

    with pytest.raises(ValidationError, match="required core case IDs"):
        suite.model_copy(update={"cases": suite.cases[:-1]}).model_validate(
            {"version": "v2", "cases": [case.model_dump() for case in suite.cases[:-1]]}
        )
