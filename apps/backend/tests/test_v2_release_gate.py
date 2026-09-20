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
    V2VerifiedMetrics,
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
    verified_by_case = {
        "provider_failure_retry_exhaustion": V2VerifiedMetrics(fake_evidence_count=0),
        "knowledge_isolation": V2VerifiedMetrics(scope_leakage_count=0),
        "read_only_safety": V2VerifiedMetrics(side_effect_tool_count=0),
    }
    return [
        V2ReleaseCaseResult(
            case_id=case.id,
            classification=V2CaseClassification.PASSED,
            passed=True,
            duration_ms=1.0,
            expectations=case.expectations,
            verified_metrics=verified_by_case.get(case.id, V2VerifiedMetrics()),
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
    assert assessment.metrics.fake_evidence.model_dump() == {
        "expected_max": 0,
        "observed": 0,
        "available": True,
    }
    assert assessment.metrics.side_effect_tools.model_dump() == {
        "expected_max": 0,
        "observed": 0,
        "available": True,
    }
    assert assessment.metrics.scope_leakage.model_dump() == {
        "expected_max": 0,
        "observed": 0,
        "available": True,
    }


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
    assert product_assessment.metrics.fake_evidence.observed == 0
    assert product_assessment.metrics.fake_evidence.available is True

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


def test_v2_release_gate_blocks_when_a_required_verified_count_is_unavailable() -> None:
    suite, policy = _inputs()
    results = _passed_results()
    results[2] = results[2].model_copy(update={"verified_metrics": V2VerifiedMetrics()})

    assessment = assess_v2_release_gate(suite, policy, results)

    assert assessment.status is V2ReleaseStatus.BLOCKED
    assert assessment.metrics.fake_evidence.observed is None
    assert assessment.metrics.fake_evidence.available is False


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


def test_v2_release_runner_reads_only_explicit_test_contract_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite, _ = _inputs()

    def completed_process(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = args[0]
        assert isinstance(command, list)
        targets = " ".join(command)
        facts = {
            "test_retryable_tool_failure_exhaustion_fails_without_evidence": (
                "V2_RELEASE_FACT={\"fake_evidence_count\": 0}"
            ),
            "test_formal_v2_scoped_rag_acceptance_preserves_only_authorized_citations": (
                "V2_RELEASE_FACT={\"scope_leakage_count\": 0}"
            ),
            "test_v2_tool_registry_excludes_side_effect_tools": (
                "V2_RELEASE_FACT={\"side_effect_tool_count\": 0}"
            ),
        }
        stdout = next((fact for name, fact in facts.items() if name in targets), "")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(release_module.subprocess, "run", completed_process)

    results = run_v2_release_suite(suite, backend_directory=Path.cwd())

    by_case = {result.case_id: result for result in results}
    assert by_case["provider_failure_retry_exhaustion"].verified_metrics.fake_evidence_count == 0
    assert by_case["knowledge_isolation"].verified_metrics.scope_leakage_count == 0
    assert by_case["read_only_safety"].verified_metrics.side_effect_tool_count == 0


def test_v2_release_suite_rejects_missing_required_core_case() -> None:
    suite, _ = _inputs()

    with pytest.raises(ValidationError, match="required core case IDs"):
        suite.model_copy(update={"cases": suite.cases[:-1]}).model_validate(
            {"version": "v2", "cases": [case.model_dump() for case in suite.cases[:-1]]}
        )
