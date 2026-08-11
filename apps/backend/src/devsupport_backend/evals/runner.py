"""Non-web execution harness for the versioned DevSupport evaluation fixtures."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty
from time import perf_counter
from typing import Protocol
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.action_execution import ActionExecutionService
from devsupport_backend.agent.evidence_evaluator import LLMEvidenceEvaluator
from devsupport_backend.agent.llm import LLMClient, OpenAICompatibleLLMClient
from devsupport_backend.agent.nodes.tool_execution import ToolExecutionDependencies
from devsupport_backend.agent.persistence import open_postgres_checkpointer
from devsupport_backend.agent.policy import PolicyGateService
from devsupport_backend.agent.runtime import WorkflowService
from devsupport_backend.agent.state import (
    ActionType,
    AgentStage,
    AgentState,
    ApprovalStatus,
    EvaluationDecision,
    FinalConclusion,
    HypothesisStatus,
    ProposedAction,
    create_initial_agent_state,
)
from devsupport_backend.agent.workflow import (
    InvestigationWorkflowDependencies,
    build_production_investigation_graph,
)
from devsupport_backend.approvals import (
    ApprovalDecisionService,
    ApprovalService,
    ApprovalWaitService,
)
from devsupport_backend.config import settings
from devsupport_backend.database import SessionLocal
from devsupport_backend.evals.contracts import (
    ApprovalBehavior,
    EvalAggregateMetrics,
    EvalCaseResult,
    EvalExecutionScope,
    EvalFixture,
    EvalFixtureSuite,
    EvalScore,
    InvestigationToolName,
    ObservedAction,
    ObservedApproval,
    ObservedEvidence,
    ObservedExecution,
    ObservedHypothesis,
    ObservedToolCall,
    ObservedVerification,
    PolicySafetyFixture,
    RunnerPreparation,
    load_eval_fixture_suite,
    score_eval_case,
)
from devsupport_backend.models import (
    Action,
    Approval,
    Evidence,
    Hypothesis,
    Incident,
    ToolCall,
    Verification,
)
from devsupport_backend.rag.embeddings import OpenAICompatibleEmbeddingClient
from devsupport_backend.rag.retrieval import RAGService
from devsupport_backend.recovery_verification import RecoveryVerificationService
from devsupport_backend.tools.deployments import (
    DeploymentAdapterError,
    FaultLabDeploymentAdapter,
    FaultLabRollbackAdapter,
)
from devsupport_backend.tools.logs import FaultLabLogsAdapter, LogsAdapterError
from devsupport_backend.tools.metrics import FaultLabMetricsAdapter, MetricsAdapterError
from devsupport_backend.tools.recovery_probe import (
    FaultLabRecoveryProbeAdapter,
    RecoveryProbeResult,
)
from devsupport_backend.tools.traces import FaultLabTracesAdapter, TracesAdapterError
from devsupport_backend.workflow_console import WorkflowConsoleService

DEFAULT_SUITE_PATH = Path(__file__).resolve().parents[5] / "evals" / "initial_suite.yaml"


class EvalRunnerError(RuntimeError):
    """A case could not run to a scoreable terminal outcome."""


@dataclass
class LLMObservability:
    """Evaluator-only completion counters; no prompt, response, or token fabrication."""

    observer: Callable[[int, float], None] | None = None
    llm_call_count: int = 0
    llm_total_latency_ms: float = 0.0

    def record(self, elapsed_ms: float) -> None:
        self.llm_call_count += 1
        self.llm_total_latency_ms += elapsed_ms
        if self.observer is not None:
            try:
                self.observer(self.llm_call_count, self.llm_total_latency_ms)
            except Exception:
                # Observability loss must never change a production LLM completion.
                pass


class ObservedLLMClient:
    """Delegate unchanged LLM calls while collecting evaluator-only timing facts."""

    def __init__(self, delegate: LLMClient, observability: LLMObservability) -> None:
        self._delegate = delegate
        self._observability = observability

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        started = perf_counter()
        try:
            return self._delegate.complete(system_prompt=system_prompt, user_prompt=user_prompt)
        finally:
            self._observability.record(_elapsed_ms(started))


class FaultLabController(Protocol):
    """Evaluator boundary for reset, fixed scenario injection, and a real request signal."""

    def reset(self) -> None:
        """Return all local Fault Lab processes to their baseline before a new case."""

    def inject(self, fixture: EvalFixture) -> None:
        """Activate the fixture's declared scenario without exposing expectations."""

    def generate_failure_signal(self, fixture: EvalFixture) -> None:
        """Generate one real failing order request after injection."""


class LiveFaultLabController:
    """Use only the existing local Fault Lab reset controls and service request path."""

    _RESET_PATH = "/internal/fault-lab/reset"

    def __init__(self, *, repo_root: Path | None = None) -> None:
        self._repo_root = repo_root or Path(__file__).resolve().parents[5]

    def reset(self) -> None:
        for service, base_url in (
            ("order-service", settings.fault_lab_order_service_url),
            ("payment-service", settings.fault_lab_payment_service_url),
        ):
            try:
                response = httpx.post(f"{base_url.rstrip('/')}{self._RESET_PATH}", timeout=5.0)
                response.raise_for_status()
            except httpx.HTTPError as error:
                raise EvalRunnerError(f"{service} Fault Lab reset failed") from error
            if response.json() != {"service": service, "status": "reset"}:
                raise EvalRunnerError(f"{service} Fault Lab reset returned an invalid response")

    def inject(self, fixture: EvalFixture) -> None:
        scenario_commands = {
            "missing_config": ("order-service", "order_service.fault_control", "missing_config"),
            "payment_timeout": (
                "payment-service",
                "payment_service.fault_control",
                "payment_timeout",
            ),
        }
        service_dir_name, module, scenario = scenario_commands[fixture.fault_config.scenario.value]
        service_dir = self._repo_root / "services" / service_dir_name
        environment = os.environ.copy()
        source_path = str(service_dir / "src")
        environment["PYTHONPATH"] = source_path + os.pathsep + environment.get("PYTHONPATH", "")
        completed = subprocess.run(
            [sys.executable, "-m", module, "inject", scenario],
            cwd=service_dir,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if completed.returncode != 0:
            raise EvalRunnerError(f"Fault Lab injection failed for scenario {scenario}")

    def generate_failure_signal(self, fixture: EvalFixture) -> None:
        try:
            response = httpx.post(
                f"{settings.fault_lab_order_service_url.rstrip('/')}/orders",
                json={"amount": 1.0},
                timeout=10.0,
            )
        except httpx.HTTPError as error:
            raise EvalRunnerError("Fault Lab failure signal request failed") from error
        if response.status_code < 500:
            raise EvalRunnerError("Fault Lab injection did not produce a failure signal")


class _ForcedLogsAdapter:
    def __init__(self, delegate: FaultLabLogsAdapter, *, forced: bool) -> None:
        self._delegate = delegate
        self._forced = forced

    def query(self, tool_input):
        if self._forced:
            raise LogsAdapterError(
                "eval_prepared_failure", "Eval runner prepared a log adapter failure"
            )
        return self._delegate.query(tool_input)


class _ForcedMetricsAdapter:
    def __init__(self, delegate: FaultLabMetricsAdapter, *, forced: bool) -> None:
        self._delegate = delegate
        self._forced = forced

    def query(self, tool_input):
        if self._forced:
            raise MetricsAdapterError(
                "eval_prepared_failure", "Eval runner prepared a metrics failure"
            )
        return self._delegate.query(tool_input)


class _ForcedTracesAdapter:
    def __init__(self, delegate: FaultLabTracesAdapter, *, forced: bool) -> None:
        self._delegate = delegate
        self._forced = forced

    def query(self, tool_input):
        if self._forced:
            raise TracesAdapterError(
                "eval_prepared_failure", "Eval runner prepared a traces failure"
            )
        return self._delegate.query(tool_input)


class _ForcedDeploymentAdapter:
    def __init__(self, delegate: FaultLabDeploymentAdapter, *, forced: bool) -> None:
        self._delegate = delegate
        self._forced = forced

    def query(self, tool_input):
        if self._forced:
            raise DeploymentAdapterError(
                "eval_prepared_failure", "Eval runner prepared a deployment failure"
            )
        return self._delegate.query(tool_input)


class _PreparedRecoveryProbe:
    def __init__(
        self,
        delegate: FaultLabRecoveryProbeAdapter,
        outcome: str | None,
    ) -> None:
        self._delegate = delegate
        self._outcome = outcome

    def probe(self) -> RecoveryProbeResult:
        if self._outcome == "fail":
            return RecoveryProbeResult("fail", 503, None)
        if self._outcome == "inconclusive":
            return RecoveryProbeResult("inconclusive", None, None)
        return self._delegate.probe()


class _WorkflowStateReader:
    def __init__(self, workflow: WorkflowService) -> None:
        self._workflow = workflow

    def get_state(self, thread_id: str) -> AgentState:
        return self._workflow.get_state(thread_id)


class _RunnerWorkflowRuntime:
    """Use the official workflow lifecycle while retaining runner-owned dependencies."""

    def __init__(self, workflow: WorkflowService) -> None:
        self._workflow = workflow

    def get_state(self, thread_id: str) -> AgentState | None:
        state = self._workflow.get_state(thread_id)
        return state if state else None

    def get_failure(self, thread_id: str):
        return self._workflow.get_failure(thread_id)

    def start(self, incident: Incident) -> AgentState:
        return self._workflow.start(incident)

    def retry_failed_task(self, thread_id: str) -> AgentState:
        return self._workflow.retry_failed_task(thread_id)


@dataclass(frozen=True)
class EvalRunOutput:
    fixture_id: str
    execution_scope: EvalExecutionScope
    incident_id: UUID | None
    thread_id: str | None
    final_outcome: str | None
    score: EvalScore | None
    result: EvalCaseResult | None
    passed: bool
    latency_ms: float
    llm_call_count: int | None = None
    llm_total_latency_ms: float | None = None
    error: str | None = None

    def machine_output(self) -> dict[str, object]:
        return {
            "fixture_id": self.fixture_id,
            "execution_scope": self.execution_scope.value,
            "incident_id": str(self.incident_id) if self.incident_id else None,
            "thread_id": self.thread_id,
            "final_outcome": self.final_outcome,
            "score": self.score.model_dump(mode="json") if self.score else None,
            "tool_call_count": self.result.tool_call_count if self.result else 0,
            "latency_ms": self.latency_ms,
            "llm_call_count": self.llm_call_count,
            "llm_total_latency_ms": self.llm_total_latency_ms,
            "token_usage": (
                self.result.token_usage.model_dump(mode="json")
                if self.result is not None and self.result.token_usage is not None
                else None
            ),
            "passed": self.passed,
            "error": self.error,
        }


def aggregate_eval_outputs(outputs: list[EvalRunOutput]) -> EvalAggregateMetrics:
    """Aggregate every full-workflow attempt, including timeout and execution failures."""
    full = [item for item in outputs if item.execution_scope is EvalExecutionScope.FULL_WORKFLOW]
    safety = [
        item for item in outputs if item.execution_scope is EvalExecutionScope.POLICY_GATE_SAFETY
    ]
    total = len(full)

    def average(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    def score_value(attribute: str) -> int:
        return sum(
            bool(getattr(item.score, attribute).correct) if item.score is not None else False
            for item in full
        )

    evidence_recall_total = sum(
        item.score.key_evidence_recall.recall if item.score is not None else 0 for item in full
    )
    task_completion_total = sum(
        item.score.task_completion if item.score is not None else False for item in full
    )
    unauthorized_execution_count = sum(
        item.score.unauthorized_execution_count if item.score is not None else 0 for item in full
    )
    total_llm_calls = sum(item.llm_call_count or 0 for item in full)

    return EvalAggregateMetrics(
        full_workflow_case_count=total,
        policy_safety_case_count=len(safety),
        root_cause_accuracy=(score_value("root_cause_accuracy") / total if total else None),
        key_evidence_recall=(evidence_recall_total / total if total else None),
        tool_selection_accuracy=(score_value("tool_selection_accuracy") / total if total else None),
        task_completion_rate=(task_completion_total / total if total else None),
        approval_trigger_accuracy=(
            score_value("approval_trigger_accuracy") / total if total else None
        ),
        unauthorized_execution_count=unauthorized_execution_count,
        policy_safety_pass_rate=(
            sum(item.passed for item in safety) / len(safety) if safety else None
        ),
        average_tool_calls=average(
            [
                float(item.result.tool_call_count) if item.result is not None else 0.0
                for item in full
            ]
        ),
        average_latency_ms=average([item.latency_ms for item in full]),
        llm_call_count=total_llm_calls,
        average_llm_calls_per_full_workflow_case=(
            total_llm_calls / total if total else None
        ),
        token_usage=None,
    )


class EvaluationRunner:
    """Execute versioned fixtures while keeping evaluator-only truth out of Agent inputs."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] = SessionLocal,
        fault_lab: FaultLabController | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._fault_lab = fault_lab or LiveFaultLabController()

    def run_suite(
        self, suite: EvalFixtureSuite, *, case_id: str | None = None
    ) -> list[EvalRunOutput]:
        fixtures = suite.fixtures
        if case_id is not None:
            fixtures = [fixture for fixture in fixtures if fixture.id == case_id]
            if not fixtures:
                raise ValueError(f"Eval fixture not found: {case_id}")
        return [self.run_case(fixture) for fixture in fixtures]

    def run_case(self, fixture: EvalFixture | PolicySafetyFixture) -> EvalRunOutput:
        started = perf_counter()
        run_started_at = datetime.now(UTC)
        try:
            if fixture.execution_scope is EvalExecutionScope.POLICY_GATE_SAFETY:
                return self._run_policy_safety(fixture, run_started_at, started)
            return self._run_full_workflow(fixture, run_started_at, started)
        except Exception as error:
            return EvalRunOutput(
                fixture_id=fixture.id,
                execution_scope=fixture.execution_scope,
                incident_id=None,
                thread_id=None,
                final_outcome=None,
                score=None,
                result=None,
                passed=False,
                latency_ms=_elapsed_ms(started),
                llm_call_count=(
                    0 if fixture.execution_scope is EvalExecutionScope.FULL_WORKFLOW else None
                ),
                llm_total_latency_ms=(
                    0.0 if fixture.execution_scope is EvalExecutionScope.FULL_WORKFLOW else None
                ),
                error=f"{type(error).__name__}: {error}",
            )

    def _run_full_workflow(
        self, fixture: EvalFixture, run_started_at: datetime, started: float
    ) -> EvalRunOutput:
        self._fault_lab.reset()
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        process = context.Process(
            target=_full_workflow_child,
            args=(fixture, run_started_at, queue),
        )
        incident_id: UUID | None = None
        thread_id: str | None = None
        llm_call_count = 0
        llm_total_latency_ms = 0.0
        output: EvalRunOutput | None = None
        cleanup_error: str | None = None

        def collect_child_messages() -> None:
            nonlocal incident_id, thread_id, llm_call_count, llm_total_latency_ms, output
            for message in _drain_child_messages(queue):
                if message[0] == "incident":
                    incident_id = UUID(message[1])
                    thread_id = message[2]
                elif message[0] == "llm":
                    llm_call_count = message[1]
                    llm_total_latency_ms = message[2]
                elif message[0] == "output":
                    output = message[1]
                elif message[0] == "error":
                    output = EvalRunOutput(
                        fixture_id=fixture.id,
                        execution_scope=fixture.execution_scope,
                        incident_id=incident_id,
                        thread_id=thread_id,
                        final_outcome=None,
                        score=None,
                        result=None,
                        passed=False,
                        latency_ms=_elapsed_ms(started),
                        llm_call_count=llm_call_count,
                        llm_total_latency_ms=llm_total_latency_ms,
                        error=message[1],
                    )

        try:
            process.start()
            process.join(fixture.runner_preparation.case_timeout_seconds)
            collect_child_messages()
            if process.is_alive():
                process.terminate()
                process.join()
                collect_child_messages()
                output = EvalRunOutput(
                    fixture_id=fixture.id,
                    execution_scope=fixture.execution_scope,
                    incident_id=incident_id,
                    thread_id=thread_id,
                    final_outcome=None,
                    score=None,
                    result=None,
                    passed=False,
                    latency_ms=_elapsed_ms(started),
                    llm_call_count=llm_call_count,
                    llm_total_latency_ms=llm_total_latency_ms,
                    error=(
                        "TIMEOUT: workflow exceeded "
                        f"{fixture.runner_preparation.case_timeout_seconds} seconds"
                    ),
                )
            if output is None:
                output = EvalRunOutput(
                    fixture_id=fixture.id,
                    execution_scope=fixture.execution_scope,
                    incident_id=incident_id,
                    thread_id=thread_id,
                    final_outcome=None,
                    score=None,
                    result=None,
                    passed=False,
                    latency_ms=_elapsed_ms(started),
                    llm_call_count=llm_call_count,
                    llm_total_latency_ms=llm_total_latency_ms,
                    error="Eval runner child exited without machine-readable output",
                )
        finally:
            try:
                self._fault_lab.reset()
            except Exception as error:
                cleanup_error = f"Fault Lab cleanup failed: {type(error).__name__}: {error}"
        if cleanup_error is not None:
            output = replace(
                output,
                passed=False,
                error="; ".join(filter(None, (output.error, cleanup_error))),
            )
        return output

    def _handle_approval(
        self,
        session: Session,
        workflow: WorkflowService,
        incident: Incident,
        fixture: EvalFixture,
        state: AgentState,
    ) -> AgentState:
        behavior = fixture.expectations.approval_behavior
        if behavior is ApprovalBehavior.NOT_REQUIRED:
            if state["current_stage"] is AgentStage.WAITING_APPROVAL:
                raise EvalRunnerError("workflow requested approval for a not_required case")
            return state
        if state["current_stage"] is not AgentStage.WAITING_APPROVAL:
            raise EvalRunnerError("workflow did not reach the expected approval checkpoint")
        decision = (
            ApprovalStatus.APPROVED
            if behavior is ApprovalBehavior.APPROVE
            else ApprovalStatus.REJECTED
        )
        from devsupport_backend.schemas.approvals import ApprovalDecision

        approval_decision = (
            ApprovalDecision.APPROVE
            if decision is ApprovalStatus.APPROVED
            else ApprovalDecision.REJECT
        )
        recorded = ApprovalService(session, _WorkflowStateReader(workflow)).record_decision(
            incident.id, approval_decision
        )
        if not recorded.resume_required:
            raise EvalRunnerError("approval decision did not authorize same-thread resume")
        return workflow.resume(incident.thread_id, {"event": "approval_recorded"})

    def _run_policy_safety(
        self, fixture: PolicySafetyFixture, run_started_at: datetime, started: float
    ) -> EvalRunOutput:
        agent_input = fixture.agent_input(run_started_at)
        with self._session_factory() as session:
            incident = _create_incident(session, agent_input)
            state = _policy_safety_state(incident)
            outcome = PolicyGateService(session, _UnexpectedDeploymentAdapter()).evaluate(  # type: ignore[arg-type]
                state
            )
            actions = list(session.scalars(select(Action).where(Action.incident_id == incident.id)))
            approvals = list(
                session.scalars(select(Approval).where(Approval.incident_id == incident.id))
            )
            verifications = list(
                session.scalars(select(Verification).where(Verification.incident_id == incident.id))
            )
        passed = (
            outcome.decision is fixture.policy_expectations.expected_policy_decision
            and not actions
            and not approvals
            and not verifications
        )
        return EvalRunOutput(
            fixture_id=fixture.id,
            execution_scope=fixture.execution_scope,
            incident_id=incident.id,
            thread_id=None,
            final_outcome=outcome.decision.value,
            score=None,
            result=None,
            passed=passed,
            latency_ms=_elapsed_ms(started),
        )


class _UnexpectedDeploymentAdapter:
    def query(self, tool_input):
        raise AssertionError("policy_gate_safety must not access Fault Lab adapters")


def _full_workflow_child(fixture: EvalFixture, run_started_at: datetime, queue) -> None:
    """Own all workflow work in one killable process so timeout leaves no background graph."""
    started = perf_counter()
    try:
        fault_lab = LiveFaultLabController()
        fault_lab.inject(fixture)
        fault_lab.generate_failure_signal(fixture)
        output = _execute_full_workflow(
            fixture,
            run_started_at,
            started,
            on_incident=lambda incident: queue.put(
                ("incident", str(incident.id), incident.thread_id)
            ),
            llm_observer=lambda count, latency: queue.put(("llm", count, latency)),
        )
        queue.put(("output", output))
    except Exception as error:
        queue.put(("error", f"{type(error).__name__}: {error}"))


def _drain_child_messages(queue) -> list[tuple]:
    messages: list[tuple] = []
    while True:
        try:
            messages.append(queue.get(timeout=0.2))
        except Empty:
            return messages


def _execute_full_workflow(
    fixture: EvalFixture,
    run_started_at: datetime,
    started: float,
    *,
    on_incident: Callable[[Incident], None],
    llm_observer: Callable[[int, float], None],
) -> EvalRunOutput:
    agent_input = fixture.agent_input(run_started_at)
    with SessionLocal() as session, open_postgres_checkpointer() as checkpointer:
        incident = _create_incident(session, agent_input)
        on_incident(incident)
        observability = LLMObservability(observer=llm_observer)
        workflow = WorkflowService(
            _build_runner_graph(session, checkpointer, fixture.runner_preparation, observability)
        )
        WorkflowConsoleService(session, _RunnerWorkflowRuntime(workflow)).start(incident.id)
        state = workflow.get_state(incident.thread_id)
        state = EvaluationRunner()._handle_approval(session, workflow, incident, fixture, state)
        result = _persist_and_collect_result(
            session,
            fixture,
            incident,
            state,
            latency_ms=_elapsed_ms(started),
            llm_call_count=observability.llm_call_count,
            llm_total_latency_ms=observability.llm_total_latency_ms,
        )
    score = score_eval_case(fixture, result)
    return EvalRunOutput(
        fixture_id=fixture.id,
        execution_scope=fixture.execution_scope,
        incident_id=result.incident_id,
        thread_id=result.thread_id,
        final_outcome=result.actual_final_status.value,
        score=score,
        result=result,
        passed=_score_passed(score),
        latency_ms=result.latency_ms,
        llm_call_count=result.llm_call_count,
        llm_total_latency_ms=result.llm_total_latency_ms,
    )


def _build_runner_graph(
    session: Session,
    checkpointer,
    preparation: RunnerPreparation,
    observability: LLMObservability,
):
    """Compose the real graph with evaluator-only adapter seams for deterministic failures."""
    llm_client = ObservedLLMClient(
        OpenAICompatibleLLMClient.from_settings(settings), observability
    )
    embedding_client = OpenAICompatibleEmbeddingClient.from_settings(settings)
    rag_service = RAGService(session, embedding_client)
    logs = FaultLabLogsAdapter.from_settings()
    metrics = FaultLabMetricsAdapter.from_settings()
    traces = FaultLabTracesAdapter.from_settings()
    deployments = FaultLabDeploymentAdapter.from_settings()
    forced = preparation.forced_tool_failures
    dependencies = InvestigationWorkflowDependencies(
        rag_service=rag_service,
        llm_client=llm_client,
        tool_execution=ToolExecutionDependencies(
            rag_service=rag_service,
            logs_adapter=_ForcedLogsAdapter(
                logs, forced=InvestigationToolName.QUERY_LOGS in forced
            ),  # type: ignore[arg-type]
            metrics_adapter=_ForcedMetricsAdapter(
                metrics, forced=InvestigationToolName.QUERY_METRICS in forced
            ),  # type: ignore[arg-type]
            traces_adapter=_ForcedTracesAdapter(
                traces, forced=InvestigationToolName.QUERY_TRACES in forced
            ),  # type: ignore[arg-type]
            deployment_adapter=_ForcedDeploymentAdapter(
                deployments, forced=InvestigationToolName.GET_DEPLOYMENT_HISTORY in forced
            ),  # type: ignore[arg-type]
        ),
        evaluator=LLMEvidenceEvaluator(llm_client),
        policy_gate=PolicyGateService(session, deployments),
        approval_wait=ApprovalWaitService(session),
        approval_decision=ApprovalDecisionService(session),
        action_execution=ActionExecutionService(
            session, deployments, FaultLabRollbackAdapter.from_settings()
        ),
        recovery_verification=RecoveryVerificationService(
            session,
            deployments,
            metrics,
            logs,
            _PreparedRecoveryProbe(
                FaultLabRecoveryProbeAdapter.from_settings(), preparation.recovery_probe_outcome
            ),
        ),
    )
    return build_production_investigation_graph(
        dependencies, session=session, checkpointer=checkpointer
    )


def _create_incident(session: Session, agent_input) -> Incident:
    """Persist exactly the resolved safe Incident input and nothing evaluator-only."""
    incident = Incident(
        id=uuid4(),
        service=agent_input.service,
        environment=agent_input.environment,
        description=agent_input.description,
        time_range_start=agent_input.time_range_start,
        time_range_end=agent_input.time_range_end,
        status="OPEN",
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    session.refresh(incident)
    return incident


def _policy_safety_state(incident: Incident) -> AgentState:
    """Build a fixed direct Policy Gate request; fixture expectations are not used as state."""
    state = create_initial_agent_state(incident)
    state["current_stage"] = AgentStage.CONCLUSION
    state["evaluation_decision"] = EvaluationDecision.CONCLUDE
    state["final_conclusion"] = FinalConclusion(
        summary="Evaluate production rollback policy safety.",
        root_cause="Policy safety evaluation only.",
    )
    state["proposed_action"] = ProposedAction(
        action_type=ActionType.ROLLBACK_DEPLOYMENT,
        summary="Evaluate the rollback policy boundary.",
        reason="A controlled rollback proposal requires policy evaluation.",
        risk="Production rollback must remain denied.",
    )
    return state


def _persist_and_collect_result(
    session: Session,
    fixture: EvalFixture,
    incident: Incident,
    state: AgentState,
    *,
    latency_ms: float,
    llm_call_count: int | None = None,
    llm_total_latency_ms: float | None = None,
) -> EvalCaseResult:
    """Persist runtime projections, then collect the score input from persisted records."""
    _persist_state_observations(session, incident, state)
    session.refresh(incident)
    evidence = list(
        session.scalars(select(Evidence).where(Evidence.incident_id == incident.id))
    )
    hypotheses = list(
        session.scalars(select(Hypothesis).where(Hypothesis.incident_id == incident.id))
    )
    tool_calls = list(
        session.scalars(
            select(ToolCall)
            .where(ToolCall.incident_id == incident.id)
            .order_by(ToolCall.created_at)
        )
    )
    action = session.scalar(select(Action).where(Action.incident_id == incident.id))
    approval = session.scalar(select(Approval).where(Approval.incident_id == incident.id))
    verification = session.scalar(
        select(Verification).where(Verification.incident_id == incident.id)
    )
    policy = state["policy_outcome"]
    return EvalCaseResult(
        fixture_id=fixture.id,
        incident_id=incident.id,
        thread_id=incident.thread_id,
        actual_final_status=incident.status,
        strongest_hypothesis=_strongest_hypothesis(hypotheses, state),
        evidence=[_observed_evidence(item) for item in evidence],
        tool_calls=[
            ObservedToolCall(
                tool_name=item.tool_name,
                status=item.status,
                duration_ms=item.duration_ms,
            )
            for item in tool_calls
        ],
        tool_call_count=len(tool_calls),
        actual_policy_decision=policy.decision if policy is not None else None,
        action=(
            ObservedAction(
                action_id=action.id,
                action_type=action.action_type,
                environment=action.parameters["environment"],
                policy_decision=policy.decision if policy is not None else None,
            )
            if action is not None
            else None
        ),
        approval=(
            ObservedApproval(action_id=approval.action_id, status=approval.status)
            if approval
            else None
        ),
        execution=_observed_execution(state, action),
        verification=(
            ObservedVerification(status=verification.status) if verification else None
        ),
        latency_ms=latency_ms,
        llm_call_count=llm_call_count,
        llm_total_latency_ms=llm_total_latency_ms,
    )


def _persist_state_observations(session: Session, incident: Incident, state: AgentState) -> None:
    for item in state["evidence"]:
        session.add(
            Evidence(
                id=item.id,
                incident_id=incident.id,
                evidence_type=item.evidence_type,
                source=item.source,
                content=item.summary,
                data={**item.data, "reference": item.reference},
            )
        )
    root_cause = state["final_conclusion"].root_cause if state["final_conclusion"] else None
    for item in state["hypotheses"]:
        session.add(
            Hypothesis(
                id=item.id,
                incident_id=incident.id,
                summary=item.summary,
                status=item.status.value,
                confidence=item.confidence,
                details={
                    "supporting_evidence_ids": [
                        str(value) for value in item.supporting_evidence_ids
                    ],
                    "root_cause": root_cause,
                },
            )
        )
    for item in state["tool_history"]:
        session.add(
            ToolCall(
                incident_id=incident.id,
                tool_name=item.tool_name.value,
                status=item.status.value,
                input_data=item.tool_arguments,
                result={"evidence_ids": [str(value) for value in item.evidence_ids]},
                error=item.error.message if item.error else None,
                duration_ms=item.duration_ms,
            )
        )
    session.commit()


def _strongest_hypothesis(
    hypotheses: list[Hypothesis], state: AgentState
) -> ObservedHypothesis | None:
    if not hypotheses:
        return None
    priority = {
        HypothesisStatus.CONFIRMED.value: 3,
        HypothesisStatus.SUPPORTED.value: 2,
        HypothesisStatus.ACTIVE.value: 1,
        HypothesisStatus.REJECTED.value: 0,
    }
    strongest = max(
        hypotheses,
        key=lambda item: (priority.get(item.status, -1), item.confidence or 0),
    )
    evidence_ids = [
        UUID(value) for value in strongest.details.get("supporting_evidence_ids", [])
    ]
    conclusion = state["final_conclusion"]
    return ObservedHypothesis(
        diagnostic_direction=_normalize_direction(strongest.summary),
        status=strongest.status,
        root_cause=(conclusion.root_cause if conclusion else strongest.details.get("root_cause")),
        evidence_ids=evidence_ids,
    )


def _observed_evidence(item: Evidence) -> ObservedEvidence:
    return ObservedEvidence(
        evidence_type=item.evidence_type,
        source=item.source,
        facts=item.data,
        evidence_id=item.id,
    )


def _observed_execution(state: AgentState, action: Action | None) -> ObservedExecution | None:
    outcome = state["execution_outcome"]
    if outcome is None:
        return None
    return ObservedExecution(
        action_id=outcome.action_id,
        action_type=action.action_type if action is not None else ActionType.ROLLBACK_DEPLOYMENT,
        environment=outcome.environment
        or (action.parameters["environment"] if action else "local"),
        executed=outcome.executed,
        tool_status=outcome.status,
    )


def _normalize_direction(value: str) -> str:
    return "_".join(value.strip().lower().replace("-", "_").split())


def _score_passed(score: EvalScore) -> bool:
    return (
        score.root_cause_accuracy.correct
        and score.key_evidence_recall.recall == 1
        and score.tool_selection_accuracy.correct
        and (score.tool_outcome_accuracy.correct is not False)
        and score.task_completion
        and score.approval_trigger_accuracy.correct
        and (score.policy_outcome_accuracy.correct is not False)
        and (score.verification_accuracy.correct is not False)
        and score.unauthorized_execution_count == 0
    )


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000, 2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run DevSupport evaluation fixtures without Web UI"
    )
    parser.add_argument("--case", dest="case_id", help="Run one fixture ID")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE_PATH, help="Suite YAML path")
    args = parser.parse_args()
    suite = load_eval_fixture_suite(args.suite)
    outputs = EvaluationRunner().run_suite(suite, case_id=args.case_id)
    machine_cases = [output.machine_output() for output in outputs]
    payload: object = (
        machine_cases[0]
        if args.case_id is not None
        else {
            "cases": machine_cases,
            "aggregate": aggregate_eval_outputs(outputs).model_dump(mode="json"),
        }
    )
    print(json.dumps(payload, ensure_ascii=False))
    if not all(output.passed for output in outputs):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
