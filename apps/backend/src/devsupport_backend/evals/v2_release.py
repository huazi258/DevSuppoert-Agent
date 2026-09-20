"""Deterministic V2 release-evaluation runner and read-only release gate.

This module intentionally does not import the V1 fixture, approval, execution,
or recovery-verification contracts.  Each V2 case invokes a focused, deterministic
pytest contract that exercises the existing V2 runtime boundary and reports only
safe, machine-readable release facts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from time import perf_counter

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_SUITE_PATH = REPOSITORY_ROOT / "evals" / "v2_release_suite.yaml"
DEFAULT_GATE_PATH = REPOSITORY_ROOT / "evals" / "v2_release_gate.yaml"
DEFAULT_RESULT_PATH = REPOSITORY_ROOT / "evals" / "results" / "v2-release-gate.json"
REQUIRED_CASE_IDS = frozenset(
    {
        "successful_investigation",
        "insufficient_evidence",
        "provider_failure_retry_exhaustion",
        "budget_exhaustion",
        "knowledge_isolation",
        "adapter_boundary",
        "round_continuation",
        "read_only_safety",
    }
)


class V2ReleaseStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class V2CaseClassification(StrEnum):
    PASSED = "passed"
    PRODUCT_FAILURE = "product_failure"
    EXTERNAL_PROVIDER_BLOCKED = "external_provider_blocked"
    EVAL_INFRASTRUCTURE_BLOCKED = "eval_infrastructure_blocked"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class V2CaseExpectations(_Model):
    """Assertions a deterministic case proves when its test target passes."""

    terminal_statuses: list[str] = Field(default_factory=list)
    terminal_reason_complete: bool = False
    evidence_grounding_correct: bool = False
    citation_complete: bool = False
    scope_isolation_correct: bool = False
    budget_compliant: bool = False
    fake_evidence_count: int = Field(default=0, ge=0)
    side_effect_tool_count: int = Field(default=0, ge=0)


class V2ReleaseCase(_Model):
    id: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    test_targets: list[str] = Field(min_length=1, max_length=12)
    expectations: V2CaseExpectations
    deterministic: bool = True

    @model_validator(mode="after")
    def validate_test_targets(self) -> "V2ReleaseCase":
        if not self.deterministic:
            raise ValueError("V2 release core cases must be deterministic")
        if len(self.test_targets) != len(set(self.test_targets)):
            raise ValueError("test_targets must be unique within a V2 release case")
        for target in self.test_targets:
            if not target.startswith("tests/") or "::test_" not in target:
                raise ValueError("test targets must select a focused backend pytest test")
        return self


class V2ReleaseSuite(_Model):
    version: str = "v2"
    cases: list[V2ReleaseCase] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_core_case_set(self) -> "V2ReleaseSuite":
        case_ids = [case.id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("V2 release case IDs must be unique")
        if set(case_ids) != REQUIRED_CASE_IDS:
            raise ValueError("V2 release suite must contain exactly the required core case IDs")
        return self


class V2ReleaseRequirements(_Model):
    all_deterministic_cases_must_pass: bool = True
    fake_evidence_max: int = Field(default=0, ge=0)
    side_effect_tool_max: int = Field(default=0, ge=0)
    scope_leakage_max: int = Field(default=0, ge=0)
    terminal_reason_complete_required: bool = True
    concluded_evidence_grounding_required: bool = True
    citation_completeness_required: bool = True
    budget_compliance_required: bool = True


class V2ReleaseGatePolicy(_Model):
    version: str = "v2"
    expected_case_ids: list[str] = Field(min_length=1)
    requirements: V2ReleaseRequirements

    @model_validator(mode="after")
    def validate_expected_case_ids(self) -> "V2ReleaseGatePolicy":
        if set(self.expected_case_ids) != REQUIRED_CASE_IDS:
            raise ValueError("V2 release gate must require every core case exactly once")
        if len(self.expected_case_ids) != len(set(self.expected_case_ids)):
            raise ValueError("expected_case_ids must be unique")
        return self


class V2ReleaseCaseResult(_Model):
    case_id: str
    classification: V2CaseClassification
    passed: bool
    duration_ms: float = Field(ge=0)
    expectations: V2CaseExpectations
    failure_reason: str | None = None


class V2ReleaseMetrics(_Model):
    terminal_status_correctness: bool
    terminal_reason_complete: bool
    evidence_grounding_correct: bool
    citation_completeness: bool
    scope_isolation: bool
    budget_compliance: bool
    fake_evidence_count: int | None = Field(default=None, ge=0)
    side_effect_tool_count: int | None = Field(default=None, ge=0)
    scope_leakage_count: int | None = Field(default=None, ge=0)


class V2ReleaseGateAssessment(_Model):
    version: str = "v2"
    status: V2ReleaseStatus
    expected_case_count: int = Field(ge=0)
    observed_case_count: int = Field(ge=0)
    passed_case_count: int = Field(ge=0)
    product_failure_cases: list[str] = Field(default_factory=list)
    external_provider_blocked_cases: list[str] = Field(default_factory=list)
    eval_infrastructure_blocked_cases: list[str] = Field(default_factory=list)
    metrics: V2ReleaseMetrics
    cases: list[V2ReleaseCaseResult]


def load_v2_release_suite(path: Path) -> V2ReleaseSuite:
    return V2ReleaseSuite.model_validate(_load_yaml(path))


def load_v2_release_gate(path: Path) -> V2ReleaseGatePolicy:
    return V2ReleaseGatePolicy.model_validate(_load_yaml(path))


def run_v2_release_suite(
    suite: V2ReleaseSuite,
    *,
    backend_directory: Path | None = None,
    timeout_seconds: float = 90.0,
) -> list[V2ReleaseCaseResult]:
    """Run each focused deterministic V2 contract without external providers."""
    cwd = backend_directory or REPOSITORY_ROOT / "apps" / "backend"
    results: list[V2ReleaseCaseResult] = []
    for case in suite.cases:
        started = perf_counter()
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *case.test_targets],
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            results.append(
                V2ReleaseCaseResult(
                    case_id=case.id,
                    classification=V2CaseClassification.EVAL_INFRASTRUCTURE_BLOCKED,
                    passed=False,
                    duration_ms=_elapsed_ms(started),
                    expectations=case.expectations,
                    failure_reason="deterministic_case_timeout",
                )
            )
            continue

        if completed.returncode == 0:
            classification = V2CaseClassification.PASSED
            failure_reason = None
        elif _is_infrastructure_failure(completed):
            classification = V2CaseClassification.EVAL_INFRASTRUCTURE_BLOCKED
            failure_reason = "pytest_infrastructure_unavailable"
        else:
            classification = V2CaseClassification.PRODUCT_FAILURE
            failure_reason = "deterministic_contract_failed"
        results.append(
            V2ReleaseCaseResult(
                case_id=case.id,
                classification=classification,
                passed=classification is V2CaseClassification.PASSED,
                duration_ms=_elapsed_ms(started),
                expectations=case.expectations,
                failure_reason=failure_reason,
            )
        )
    return results


def assess_v2_release_gate(
    suite: V2ReleaseSuite,
    policy: V2ReleaseGatePolicy,
    results: list[V2ReleaseCaseResult],
) -> V2ReleaseGateAssessment:
    """Assess V2-only reliability and safety facts from deterministic contracts."""
    expected_ids = set(policy.expected_case_ids)
    expected_by_id = {case.id: case for case in suite.cases}
    results_by_id = {result.case_id: result for result in results}
    complete = (
        set(expected_by_id) == expected_ids
        and len(results) == len(expected_ids)
        and set(results_by_id) == expected_ids
    )
    all_passed = complete and all(result.passed for result in results)
    product_failure_cases = [
        result.case_id
        for result in results
        if result.classification is V2CaseClassification.PRODUCT_FAILURE
    ]
    external_provider_blocked_cases = [
        result.case_id
        for result in results
        if result.classification is V2CaseClassification.EXTERNAL_PROVIDER_BLOCKED
    ]
    eval_infrastructure_blocked_cases = [
        result.case_id
        for result in results
        if result.classification is V2CaseClassification.EVAL_INFRASTRUCTURE_BLOCKED
    ]
    if not complete:
        eval_infrastructure_blocked_cases.append("release_case_completeness")

    def proven(attribute: str) -> bool:
        relevant = [
            result
            for result in results
            if bool(getattr(result.expectations, attribute))
        ]
        return bool(relevant) and all(result.passed for result in relevant)

    terminal_status_correctness = all(
        result.passed
        for result in results
        if result.expectations.terminal_statuses
    ) and any(result.expectations.terminal_statuses for result in results)
    metrics_complete = all_passed
    metrics = V2ReleaseMetrics(
        terminal_status_correctness=terminal_status_correctness,
        terminal_reason_complete=proven("terminal_reason_complete"),
        evidence_grounding_correct=proven("evidence_grounding_correct"),
        citation_completeness=proven("citation_complete"),
        scope_isolation=proven("scope_isolation_correct"),
        budget_compliance=proven("budget_compliant"),
        fake_evidence_count=(0 if metrics_complete else None),
        side_effect_tool_count=(0 if metrics_complete else None),
        scope_leakage_count=(0 if metrics_complete else None),
    )
    requirements = policy.requirements
    requirements_passed = (
        (not requirements.all_deterministic_cases_must_pass or all_passed)
        and metrics.terminal_status_correctness
        and (
            not requirements.terminal_reason_complete_required
            or metrics.terminal_reason_complete
        )
        and (
            not requirements.concluded_evidence_grounding_required
            or metrics.evidence_grounding_correct
        )
        and (
            not requirements.citation_completeness_required
            or metrics.citation_completeness
        )
        and (not requirements.budget_compliance_required or metrics.budget_compliance)
        and metrics.fake_evidence_count is not None
        and metrics.fake_evidence_count <= requirements.fake_evidence_max
        and metrics.side_effect_tool_count is not None
        and metrics.side_effect_tool_count <= requirements.side_effect_tool_max
        and metrics.scope_leakage_count is not None
        and metrics.scope_leakage_count <= requirements.scope_leakage_max
    )
    status = (
        V2ReleaseStatus.FAIL
        if product_failure_cases
        else V2ReleaseStatus.BLOCKED
        if external_provider_blocked_cases or eval_infrastructure_blocked_cases
        else V2ReleaseStatus.PASS
        if requirements_passed
        else V2ReleaseStatus.FAIL
    )
    return V2ReleaseGateAssessment(
        status=status,
        expected_case_count=len(expected_ids),
        observed_case_count=len(results),
        passed_case_count=sum(result.passed for result in results),
        product_failure_cases=product_failure_cases,
        external_provider_blocked_cases=external_provider_blocked_cases,
        eval_infrastructure_blocked_cases=eval_infrastructure_blocked_cases,
        metrics=metrics,
        cases=results,
    )


def build_v2_release_payload(assessment: V2ReleaseGateAssessment) -> dict[str, object]:
    return {"release_gate": assessment.model_dump(mode="json")}


def _load_yaml(path: Path) -> object:
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"unable to read V2 release configuration: {path}") from error
    if not isinstance(content, dict):
        raise ValueError("V2 release configuration must be a YAML mapping")
    return content


def _is_infrastructure_failure(completed: subprocess.CompletedProcess[str]) -> bool:
    output = f"{completed.stdout}\n{completed.stderr}".lower()
    return completed.returncode == 5 or "no module named pytest" in output


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000, 2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic DevSupport V2 release gate")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE_PATH)
    parser.add_argument("--gate", type=Path, default=DEFAULT_GATE_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULT_PATH)
    args = parser.parse_args()

    suite = load_v2_release_suite(args.suite)
    policy = load_v2_release_gate(args.gate)
    assessment = assess_v2_release_gate(suite, policy, run_v2_release_suite(suite))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_v2_release_payload(assessment), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(build_v2_release_payload(assessment), ensure_ascii=False))
    if assessment.status is not V2ReleaseStatus.PASS:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
