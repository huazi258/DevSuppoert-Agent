"""Day 5.3 runner orchestration and persisted-result tests."""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Queue
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from devsupport_backend.agent.state import (
    AgentStage,
    EvidenceContext,
    HypothesisContext,
    HypothesisStatus,
    ToolHistoryEntry,
    create_initial_agent_state,
)
from devsupport_backend.evals.contracts import (
    ApprovalTriggerScore,
    EfficiencyMetrics,
    EvalCaseResult,
    EvalExecutionScope,
    EvalFinalStatus,
    EvalFixture,
    EvalScore,
    EvidenceRecallScore,
    ObservedToolCall,
    PolicyOutcomeScore,
    PolicySafetyFixture,
    RootCauseScore,
    ToolOutcomeScore,
    ToolSelectionScore,
    VerificationScore,
    load_eval_fixture_suite,
    score_eval_case,
)
from devsupport_backend.evals.runner import (
    EvalRunnerError,
    EvalRunOutput,
    EvaluationRunner,
    LLMObservability,
    ObservedLLMClient,
    _create_incident,
    _ForcedLogsAdapter,
    _persist_and_collect_result,
    aggregate_eval_outputs,
)
from devsupport_backend.models import Incident
from devsupport_backend.tools.logs import LogsAdapterError
from devsupport_backend.tools.schemas import QueryLogsInput, ToolStatus

SUITE_PATH = Path(__file__).resolve().parents[3] / "evals" / "initial_suite.yaml"
RUN_STARTED_AT = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)


class NoFaultLab:
    def reset(self) -> None:
        raise AssertionError("policy_gate_safety must not reset Fault Lab")

    def inject(self, fixture: EvalFixture) -> None:
        raise AssertionError("policy_gate_safety must not inject Fault Lab")

    def generate_failure_signal(self, fixture: EvalFixture) -> None:
        raise AssertionError("policy_gate_safety must not call Fault Lab")


class FailingFaultLab:
    def reset(self) -> None:
        raise EvalRunnerError("missing local services")

    def inject(self, fixture: EvalFixture) -> None:
        raise AssertionError("runner must stop after reset failure")

    def generate_failure_signal(self, fixture: EvalFixture) -> None:
        raise AssertionError("runner must stop after reset failure")


def _suite() -> object:
    return load_eval_fixture_suite(SUITE_PATH)


def _full_fixture(case_id: str) -> EvalFixture:
    return next(
        fixture
        for fixture in _suite().fixtures  # type: ignore[union-attr]
        if isinstance(fixture, EvalFixture) and fixture.id == case_id
    )


def test_suite_and_single_case_dispatch_without_fixture_id_behavior() -> None:
    suite = _suite()

    class DispatchRunner(EvaluationRunner):
        def __init__(self) -> None:
            self.seen: list[str] = []

        def run_case(self, fixture):
            self.seen.append(fixture.id)
            return EvalRunOutput(
                fixture_id=fixture.id,
                execution_scope=fixture.execution_scope,
                incident_id=None,
                thread_id=None,
                final_outcome=None,
                score=None,
                result=None,
                passed=True,
                latency_ms=0,
            )

    runner = DispatchRunner()
    assert len(runner.run_suite(suite)) == len(suite.fixtures)  # type: ignore[union-attr]
    assert runner.seen == [fixture.id for fixture in suite.fixtures]  # type: ignore[union-attr]
    runner.seen.clear()
    runner.run_suite(suite, case_id="a-approve-happy")  # type: ignore[arg-type]
    assert runner.seen == ["a-approve-happy"]
    with pytest.raises(ValueError, match="not found"):
        runner.run_suite(suite, case_id="not-a-case")  # type: ignore[arg-type]


def test_policy_safety_dispatch_never_accesses_fault_lab(database_session: Session) -> None:
    fixture = next(
        item
        for item in _suite().fixtures
        if isinstance(item, PolicySafetyFixture)  # type: ignore[union-attr]
    )
    runner = EvaluationRunner(
        session_factory=lambda: nullcontext(database_session),  # type: ignore[arg-type]
        fault_lab=NoFaultLab(),
    )

    output = runner.run_case(fixture)

    assert output.passed is True
    assert output.final_outcome == "DENIED"
    assert output.execution_scope.value == "policy_gate_safety"


def test_runner_preparation_is_metadata_driven_not_fixture_id() -> None:
    first = _full_fixture("a-query-logs-tool-failure")
    second = first.model_copy(update={"id": "same-preparation-different-id"})

    assert first.runner_preparation == second.runner_preparation
    adapter = _ForcedLogsAdapter(object(), forced=True)
    tool_input = QueryLogsInput(
        service="order-service",
        environment="local",
        time_range_start=RUN_STARTED_AT - timedelta(minutes=1),
        time_range_end=RUN_STARTED_AT,
    )
    with pytest.raises(LogsAdapterError, match="prepared"):
        adapter.query(tool_input)


def test_created_incident_contains_only_resolved_agent_input(database_session: Session) -> None:
    fixture = _full_fixture("a-approve-happy")
    agent_input = fixture.agent_input(RUN_STARTED_AT)

    incident = _create_incident(database_session, agent_input)

    assert {
        "service": incident.service,
        "environment": incident.environment,
        "description": incident.description,
        "time_range_start": incident.time_range_start,
        "time_range_end": incident.time_range_end,
    } == agent_input.model_dump()
    serialized = incident.description.lower()
    assert fixture.expectations.expected_diagnostic_direction.canonical_direction not in serialized
    assert fixture.expectations.approval_behavior.value not in serialized
    assert "runner_preparation" not in serialized


@pytest.mark.parametrize(
    ("case_id", "expected_decision"),
    [("a-approve-happy", "APPROVE"), ("a-approval-reject", "REJECT")],
)
def test_approval_routes_through_the_same_incident_thread(
    database_session: Session, monkeypatch: pytest.MonkeyPatch, case_id: str, expected_decision: str
) -> None:
    fixture = _full_fixture(case_id)
    incident = _create_incident(database_session, fixture.agent_input(RUN_STARTED_AT))
    state = create_initial_agent_state(incident)
    state["current_stage"] = AgentStage.WAITING_APPROVAL
    recorded: dict[str, object] = {}

    class RecordedApprovalService:
        def __init__(self, session: Session, reader: object) -> None:
            recorded["reader"] = reader

        def record_decision(self, incident_id, decision):
            recorded["incident_id"] = incident_id
            recorded["decision"] = decision.value
            return type("ApprovalResult", (), {"resume_required": True})()

    class SameThreadWorkflow:
        def get_state(self, thread_id: str):
            assert thread_id == incident.thread_id
            return state

        def resume(self, thread_id: str, payload: object):
            recorded["resume_thread_id"] = thread_id
            recorded["payload"] = payload
            return state

    monkeypatch.setattr("devsupport_backend.evals.runner.ApprovalService", RecordedApprovalService)
    result = EvaluationRunner()._handle_approval(  # noqa: SLF001
        database_session,
        SameThreadWorkflow(),
        incident,
        fixture,
        state,  # type: ignore[arg-type]
    )

    assert result is state
    assert recorded["incident_id"] == incident.id
    assert recorded["decision"] == expected_decision
    assert recorded["resume_thread_id"] == incident.thread_id
    assert recorded["payload"] == {"event": "approval_recorded"}


def test_persisted_collection_constructs_and_scores_eval_case_result(
    database_session: Session,
) -> None:
    fixture = _full_fixture("a-approve-happy")
    incident = Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        status="NEEDS_MANUAL_ACTION",
        description="A runtime failure was observed.",
        time_range_start=RUN_STARTED_AT - timedelta(minutes=5),
        time_range_end=RUN_STARTED_AT,
        thread_id=str(uuid4()),
    )
    database_session.add(incident)
    database_session.commit()
    evidence = EvidenceContext(
        evidence_type="log_query_result",
        source="query_logs",
        summary="Observed missing configuration failure.",
        data={
            "match_count": 1,
            "error_patterns": [
                {"pattern": "MissingRequiredConfiguration", "count": 1}
            ],
            "sample_count": 1,
        },
    )
    hypothesis = HypothesisContext(
        summary="missing_order_service_configuration",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.9,
        supporting_evidence_ids=[evidence.id],
    )
    state = create_initial_agent_state(incident)
    state.update(
        {
            "current_stage": AgentStage.NEEDS_MANUAL_ACTION,
            "evidence": [evidence],
            "hypotheses": [hypothesis],
            "tool_history": [
                ToolHistoryEntry(
                    tool_name="query_logs",
                    tool_arguments={"service": "order-service", "environment": "local"},
                    status=ToolStatus.SUCCESS,
                    evidence_ids=[evidence.id],
                )
            ],
        }
    )

    result = _persist_and_collect_result(
        database_session, fixture, incident, state, latency_ms=12.5
    )

    assert result.incident_id == incident.id
    assert result.tool_call_count == 1
    assert result.strongest_hypothesis is not None
    assert result.strongest_hypothesis.evidence_ids == [evidence.id]
    assert result.evidence[0].evidence_id == evidence.id
    assert result.latency_ms == 12.5
    assert result.fixture_id == fixture.id
    assert score_eval_case(fixture, result).fixture_id == fixture.id


def test_case_failure_is_reported_as_not_passed() -> None:
    output = EvaluationRunner(fault_lab=FailingFaultLab()).run_case(
        _full_fixture("a-approve-happy")
    )

    assert output.passed is False
    assert output.score is None
    assert output.error is not None
    assert "missing local services" in output.error


def _aggregate_score(*, tool_call_count: int, latency_ms: float) -> EvalScore:
    return EvalScore(
        fixture_id="aggregate-case",
        root_cause_accuracy=RootCauseScore(
            correct=True,
            diagnostic_direction_correct=True,
            grounded_conclusion_correct=True,
        ),
        key_evidence_recall=EvidenceRecallScore(covered=2, required=2, recall=1.0),
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
        unauthorized_execution_count=2,
        efficiency=EfficiencyMetrics(tool_call_count=tool_call_count, latency_ms=latency_ms),
    )


def _aggregate_result(*, tool_call_count: int, latency_ms: float) -> EvalCaseResult:
    return EvalCaseResult(
        fixture_id="aggregate-case",
        incident_id=uuid4(),
        thread_id="aggregate-thread",
        actual_final_status=EvalFinalStatus.NEEDS_MANUAL_ACTION,
        tool_calls=[
            ObservedToolCall(tool_name="query_logs", status=ToolStatus.SUCCESS)
            for _ in range(tool_call_count)
        ],
        tool_call_count=tool_call_count,
        latency_ms=latency_ms,
    )


def test_suite_aggregate_retains_failed_full_workflows_and_separates_policy_safety() -> None:
    successful_full = EvalRunOutput(
        fixture_id="aggregate-success",
        execution_scope=EvalExecutionScope.FULL_WORKFLOW,
        incident_id=uuid4(),
        thread_id="aggregate-success-thread",
        final_outcome="NEEDS_MANUAL_ACTION",
        score=_aggregate_score(tool_call_count=4, latency_ms=10.0),
        result=_aggregate_result(tool_call_count=4, latency_ms=10.0),
        passed=True,
        latency_ms=10.0,
        llm_call_count=3,
        llm_total_latency_ms=15.0,
    )
    timed_out_full = EvalRunOutput(
        fixture_id="aggregate-timeout",
        execution_scope=EvalExecutionScope.FULL_WORKFLOW,
        incident_id=uuid4(),
        thread_id="aggregate-timeout-thread",
        final_outcome=None,
        score=None,
        result=None,
        passed=False,
        latency_ms=30.0,
        llm_call_count=2,
        llm_total_latency_ms=8.0,
        error="TIMEOUT",
    )
    policy_safety = EvalRunOutput(
        fixture_id="aggregate-policy-safety",
        execution_scope=EvalExecutionScope.POLICY_GATE_SAFETY,
        incident_id=uuid4(),
        thread_id=None,
        final_outcome="DENIED",
        score=None,
        result=None,
        passed=True,
        latency_ms=5.0,
    )

    aggregate = aggregate_eval_outputs([successful_full, timed_out_full, policy_safety])

    assert aggregate.full_workflow_case_count == 2
    assert aggregate.policy_safety_case_count == 1
    assert aggregate.root_cause_accuracy == 0.5
    assert aggregate.key_evidence_recall == 0.5
    assert aggregate.tool_selection_accuracy == 0.5
    assert aggregate.task_completion_rate == 0.5
    assert aggregate.approval_trigger_accuracy == 0.5
    assert aggregate.unauthorized_execution_count == 2
    assert aggregate.policy_safety_pass_rate == 1.0
    assert aggregate.average_tool_calls == 2.0
    assert aggregate.average_latency_ms == 20.0
    assert aggregate.llm_call_count == 5
    assert aggregate.average_llm_calls_per_full_workflow_case == 2.5
    assert aggregate.token_usage is None


def test_observed_llm_client_preserves_completion_input_and_records_latency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class Delegate:
        def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            calls.append((system_prompt, user_prompt))
            return "unchanged completion"

    timestamps = iter((100.0, 100.25))
    monkeypatch.setattr(
        "devsupport_backend.evals.runner.perf_counter", lambda: next(timestamps)
    )

    def unavailable_parent_observer(_: int, __: float) -> None:
        raise RuntimeError("parent queue unavailable")

    observability = LLMObservability(observer=unavailable_parent_observer)

    response = ObservedLLMClient(Delegate(), observability).complete(
        system_prompt="trusted system prompt", user_prompt="incident context"
    )

    assert response == "unchanged completion"
    assert calls == [("trusted system prompt", "incident context")]
    assert observability.llm_call_count == 1
    assert observability.llm_total_latency_ms == 250.0


def test_suite_cli_outputs_case_results_and_aggregate(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    full_output = EvalRunOutput(
        fixture_id="cli-full",
        execution_scope=EvalExecutionScope.FULL_WORKFLOW,
        incident_id=uuid4(),
        thread_id="cli-thread",
        final_outcome="NEEDS_MANUAL_ACTION",
        score=_aggregate_score(tool_call_count=1, latency_ms=5.0),
        result=_aggregate_result(tool_call_count=1, latency_ms=5.0),
        passed=True,
        latency_ms=5.0,
        llm_call_count=1,
    )
    safety_output = EvalRunOutput(
        fixture_id="cli-safety",
        execution_scope=EvalExecutionScope.POLICY_GATE_SAFETY,
        incident_id=uuid4(),
        thread_id=None,
        final_outcome="DENIED",
        score=None,
        result=None,
        passed=True,
        latency_ms=1.0,
    )

    class CliRunner:
        def run_suite(self, suite: object, *, case_id: str | None = None) -> list[EvalRunOutput]:
            assert case_id is None
            return [full_output, safety_output]

    monkeypatch.setattr("devsupport_backend.evals.runner.EvaluationRunner", CliRunner)
    monkeypatch.setattr(
        "devsupport_backend.evals.runner.load_eval_fixture_suite", lambda _: object()
    )
    monkeypatch.setattr(sys, "argv", ["eval-runner"])

    from devsupport_backend.evals.runner import main

    main()

    payload = json.loads(capsys.readouterr().out)
    assert [case["fixture_id"] for case in payload["cases"]] == ["cli-full", "cli-safety"]
    assert payload["aggregate"]["full_workflow_case_count"] == 1
    assert payload["aggregate"]["policy_safety_case_count"] == 1


def test_timeout_returns_machine_failure_preserves_incident_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _full_fixture("a-approve-happy").model_copy(
        update={
            "runner_preparation": _full_fixture("a-approve-happy")
            .runner_preparation.model_copy(update={"case_timeout_seconds": 10})
        }
    )
    incident_id = uuid4()

    class TrackingFaultLab:
        def __init__(self) -> None:
            self.resets = 0

        def reset(self) -> None:
            self.resets += 1

        def inject(self, fixture: EvalFixture) -> None:
            raise AssertionError("child owns injection")

        def generate_failure_signal(self, fixture: EvalFixture) -> None:
            raise AssertionError("child owns signal generation")

    class LoopingProcess:
        def __init__(self, *, args, **_: object) -> None:
            self._queue = args[2]
            self._alive = True

        def start(self) -> None:
            self._queue.put(("incident", str(incident_id), "timeout-thread"))
            self._queue.put(("llm", 2, 12.5))

        def join(self, timeout: object = None) -> None:
            return None

        def is_alive(self) -> bool:
            return self._alive

        def terminate(self) -> None:
            self._alive = False

    class FakeContext:
        def Queue(self):
            return Queue()

        def Process(self, **kwargs):
            return LoopingProcess(**kwargs)

    fault_lab = TrackingFaultLab()
    monkeypatch.setattr(
        "devsupport_backend.evals.runner.multiprocessing.get_context", lambda _: FakeContext()
    )

    output = EvaluationRunner(fault_lab=fault_lab).run_case(fixture)

    assert output.passed is False
    assert output.incident_id == incident_id
    assert output.thread_id == "timeout-thread"
    assert output.error == "TIMEOUT: workflow exceeded 10 seconds"
    assert output.llm_call_count == 2
    assert output.llm_total_latency_ms == 12.5
    assert fault_lab.resets == 2
