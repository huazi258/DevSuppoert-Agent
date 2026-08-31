"""V1 release-profile selection tests over the immutable Fault Lab fixture suite."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from devsupport_backend.evals.contracts import (
    EvalReleaseProfiles,
    load_eval_fixture_suite,
    load_eval_release_profiles,
)
from devsupport_backend.evals.runner import EvaluationRunner, select_eval_fixtures

SUITE_PATH = Path(__file__).resolve().parents[3] / "evals" / "initial_suite.yaml"
PROFILE_PATH = Path(__file__).resolve().parents[3] / "evals" / "v1_release_profiles.yaml"
P0_CASE_IDS = [
    "a-approve-happy",
    "b-payment-timeout-standard",
    "a-query-logs-tool-failure",
    "a-approval-reject",
    "a-recovery-verification-failure",
    "production-policy-gate-denied",
]
EXTENDED_CASE_IDS = ["a-wording-variant", "b-wording-noise-variant"]


def _profiles():
    return load_eval_release_profiles(PROFILE_PATH, load_eval_fixture_suite(SUITE_PATH))


def test_v1_release_profiles_strictly_load_and_freeze_p0_and_extended_cases() -> None:
    profiles = _profiles()

    assert profiles.version == "v1"
    assert profiles.profiles["p0_fault_lab"].case_ids == P0_CASE_IDS
    assert profiles.profiles["extended_fault_lab"].case_ids == EXTENDED_CASE_IDS
    assert set(profiles.profiles["p0_fault_lab"].case_ids).isdisjoint(
        profiles.profiles["extended_fault_lab"].case_ids
    )
    assert profiles.profiles["p0_fault_lab"].coverage == {
        "a-approve-happy": "happy_path_remediation",
        "b-payment-timeout-standard": "second_fault_direction",
        "a-query-logs-tool-failure": "investigation_tool_failure",
        "a-approval-reject": "approval_rejection",
        "a-recovery-verification-failure": "recovery_verification_failure",
        "production-policy-gate-denied": "production_policy_denial",
    }


def test_release_profile_ids_exist_and_classify_every_initial_fixture() -> None:
    suite = load_eval_fixture_suite(SUITE_PATH)
    profiles = _profiles()
    profile_case_ids = {
        case_id for profile in profiles.profiles.values() for case_id in profile.case_ids
    }

    assert profile_case_ids == {fixture.id for fixture in suite.fixtures}
    assert "production-policy-gate-denied" in profiles.profiles["p0_fault_lab"].case_ids


def test_release_profiles_reject_duplicate_case_ids_and_unknown_fixture_references() -> None:
    payload = _profiles().model_dump(mode="json")
    payload["profiles"]["p0_fault_lab"]["case_ids"].append("a-approve-happy")  # type: ignore[index]
    with pytest.raises(ValidationError, match="case_ids must be unique"):
        EvalReleaseProfiles.model_validate(payload)

    payload = _profiles().model_dump(mode="json")
    payload["profiles"]["extended_fault_lab"]["case_ids"][1] = "unknown-fixture"  # type: ignore[index]
    with pytest.raises(ValueError, match="unknown fixture IDs: unknown-fixture"):
        EvalReleaseProfiles.model_validate(payload).require_fixture_ids(
            load_eval_fixture_suite(SUITE_PATH)
        )


def test_select_eval_fixtures_preserves_profile_order_and_fails_closed() -> None:
    suite = load_eval_fixture_suite(SUITE_PATH)
    selected = select_eval_fixtures(suite, _profiles().profiles["p0_fault_lab"].case_ids)

    assert [fixture.id for fixture in selected.fixtures] == P0_CASE_IDS
    with pytest.raises(ValueError, match="not found: unknown-fixture"):
        select_eval_fixtures(suite, ["unknown-fixture"])


def test_profile_selection_does_not_change_fixture_agent_input_or_expectations() -> None:
    suite = load_eval_fixture_suite(SUITE_PATH)
    before = {
        fixture.id: fixture.model_dump(mode="json")
        for fixture in suite.fixtures
    }

    select_eval_fixtures(suite, P0_CASE_IDS)

    assert {
        fixture.id: fixture.model_dump(mode="json") for fixture in suite.fixtures
    } == before


def test_p0_selection_runs_only_the_selected_cases() -> None:
    class DispatchRunner(EvaluationRunner):
        def __init__(self) -> None:
            self.seen: list[str] = []

        def run_case(self, fixture):
            self.seen.append(fixture.id)
            return fixture.id

    runner = DispatchRunner()
    p0_suite = select_eval_fixtures(load_eval_fixture_suite(SUITE_PATH), P0_CASE_IDS)

    assert runner.run_suite(p0_suite) == P0_CASE_IDS
    assert runner.seen == P0_CASE_IDS


def test_cli_profile_selects_p0_without_changing_default_full_suite_behavior(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    class CliRunner:
        def run_suite(self, suite, *, case_id=None):
            assert case_id is None
            seen.append([fixture.id for fixture in suite.fixtures])
            return [
                SimpleNamespace(passed=True, machine_output=lambda: {"fixture_id": fixture.id})
                for fixture in suite.fixtures
            ]

    monkeypatch.setattr("devsupport_backend.evals.runner.EvaluationRunner", CliRunner)
    monkeypatch.setattr(
        "devsupport_backend.evals.runner.aggregate_eval_outputs",
        lambda _: SimpleNamespace(model_dump=lambda **__: {}),
    )
    monkeypatch.setattr(
        "devsupport_backend.evals.runner.assess_eval_release_gate",
        lambda *_: SimpleNamespace(model_dump=lambda **__: {"status": "PASS"}),
    )
    monkeypatch.setattr(sys, "argv", ["eval-runner"])

    from devsupport_backend.evals.runner import main

    main()
    monkeypatch.setattr(sys, "argv", ["eval-runner", "--profile", "p0_fault_lab"])
    main()

    full_suite_case_ids = [fixture.id for fixture in load_eval_fixture_suite(SUITE_PATH).fixtures]
    assert seen == [full_suite_case_ids, P0_CASE_IDS]
    full_payload, p0_payload = [
        json.loads(line) for line in capsys.readouterr().out.splitlines()
    ]
    assert "release_gate" not in full_payload
    assert p0_payload["release_gate"] == {"status": "PASS"}


def test_cli_rejects_conflicting_case_and_profile_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["eval-runner", "--case", "a-approve-happy", "--profile", "p0_fault_lab"],
    )

    from devsupport_backend.evals.runner import main

    with pytest.raises(SystemExit, match="2"):
        main()
