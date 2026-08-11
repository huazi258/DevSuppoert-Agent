"""Day 5.3 runner orchestration and persisted-result tests."""

from __future__ import annotations

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
    EvalFixture,
    PolicySafetyFixture,
    load_eval_fixture_suite,
    score_eval_case,
)
from devsupport_backend.evals.runner import (
    EvalRunnerError,
    EvalRunOutput,
    EvaluationRunner,
    _create_incident,
    _ForcedLogsAdapter,
    _persist_and_collect_result,
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
    assert fault_lab.resets == 2
