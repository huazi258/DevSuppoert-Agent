"""Deterministic acceptance coverage for the formal bounded V2 investigation workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.orm import Session

import devsupport_backend.agent.workflow as workflow_module
from devsupport_backend.agent.budget import InvestigationBudget
from devsupport_backend.agent.nodes.tool_execution import ToolExecutionDependencies
from devsupport_backend.agent.runtime import WorkflowService
from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    EvaluationDecision,
    HypothesisStatus,
    TerminalReason,
    create_initial_agent_state,
)
from devsupport_backend.agent.workflow import (
    V2InvestigationWorkflowDependencies,
    build_v2_production_investigation_graph,
)
from devsupport_backend.investigation_lifecycle import InvestigationLifecycleService
from devsupport_backend.investigation_status import InvestigationStatus
from devsupport_backend.models import (
    Incident,
    InvestigationRound,
    InvestigationTarget,
    Report,
    Service,
    ToolCall,
)
from devsupport_backend.rag.retrieval import Citation, KnowledgeScope, RetrievalResult
from devsupport_backend.tools.adapter_contracts import (
    AdapterError,
    AdapterProvenance,
    LogEvent,
    LogQueryResult,
)
from devsupport_backend.tools.registry import ToolName
from devsupport_backend.tools.schemas import ToolStatus

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)


@dataclass
class _RecordingObserver:
    """Records formal graph nodes without changing their behavior."""

    started: list[str] = field(default_factory=list)

    def node_started(self, node_name: str) -> None:
        self.started.append(node_name)

    def node_finished(self, _: str, __: float, ___: str) -> None:
        return None


class _DeterministicRAG:
    """Fixed citation-backed scoped result used through the real retrieval Tool boundary."""

    def __init__(self, *, target_id: UUID, service_id: UUID) -> None:
        self.target_id = target_id
        self.service_id = service_id
        self.scopes: list[KnowledgeScope] = []
        self.document_id = uuid4()
        self.chunk_id = uuid4()

    def search_scoped(
        self,
        _: str,
        *,
        scope: KnowledgeScope,
        document_type: str | None = None,
        top_k: int = 5,
    ) -> list[RetrievalResult]:
        assert document_type is None
        assert top_k == 5
        self.scopes.append(scope)
        return [
            RetrievalResult(
                chunk_id=self.chunk_id,
                document_id=self.document_id,
                content=(
                    "Runbook: inspect the current runtime error signal before "
                    "confirming root cause."
                ),
                service="order-service",
                environment="local",
                document_type="runbook",
                source="knowledge/runbooks/order-runtime.md",
                section="Runtime checks",
                vector_score=0.9,
                keyword_score=0.9,
                fusion_score=0.9,
                citation=Citation(
                    id=f"knowledge:{self.document_id}:{self.chunk_id}",
                    document_id=self.document_id,
                    chunk_id=self.chunk_id,
                    document_title="Order service runtime runbook",
                    source="knowledge/runbooks/order-runtime.md",
                    source_path="knowledge/runbooks/order-runtime.md",
                    chunk_index=0,
                    section="Runtime checks",
                    document_version="2026.09",
                    target_id=self.target_id,
                    scope="service",
                    service_id=self.service_id,
                    environment="local",
                    document_reference="knowledge/runbooks/order-runtime.md#runtime-checks",
                ),
            )
        ]


class _DeterministicLLM:
    """Returns schema-valid generation and update payloads from only supplied state IDs."""

    def __init__(self, *, confirm: bool) -> None:
        self.confirm = confirm
        self.calls: list[dict[str, object]] = []

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        del system_prompt
        context = json.loads(user_prompt)
        self.calls.append(context)
        if "knowledge_evidence" in context:
            evidence_id = context["knowledge_evidence"][0]["id"]
            return json.dumps(
                {
                    "hypotheses": [
                        {
                            "summary": "Order service runtime configuration may be invalid.",
                            "confidence": 0.5,
                            "supporting_evidence_ids": [evidence_id],
                            "next_check": "Collect the current order-service error signal.",
                        },
                        {
                            "summary": "A downstream outage may be responsible.",
                            "confidence": 0.3,
                            "supporting_evidence_ids": [],
                            "next_check": (
                                "Compare the direct runtime evidence with dependency signals."
                            ),
                        },
                    ]
                }
            )
        hypotheses = context["hypotheses"]
        runtime_evidence_id = next(
            item["id"] for item in context["evidence"] if item["source"] == "query_logs"
        )
        knowledge_evidence_id = next(
            item["id"] for item in context["evidence"] if item["source"] == "search_knowledge"
        )
        primary_status = "CONFIRMED" if self.confirm else "ACTIVE"
        return json.dumps(
            {
                "updates": [
                    {
                        "hypothesis_id": hypotheses[0]["id"],
                        "supporting_evidence_ids": [knowledge_evidence_id, runtime_evidence_id],
                        "contradicting_evidence_ids": [],
                        "confidence": 0.95 if self.confirm else 0.5,
                        "status": primary_status,
                        "next_check": (
                            "Inspect another bounded signal if evidence remains insufficient."
                        ),
                    },
                    {
                        "hypothesis_id": hypotheses[1]["id"],
                        "supporting_evidence_ids": [],
                        "contradicting_evidence_ids": [runtime_evidence_id]
                        if self.confirm
                        else [],
                        "confidence": 0.05 if self.confirm else 0.3,
                        "status": "REJECTED" if self.confirm else "ACTIVE",
                        "next_check": (
                            "Keep investigating unless direct evidence resolves the hypothesis."
                        ),
                    },
                ]
            }
        )


class _NormalLogsAdapter:
    """One deterministic runtime fact produced through the normal adapter contract."""

    def __init__(self) -> None:
        self.calls = 0

    def query(self, _: object) -> LogQueryResult:
        self.calls += 1
        return LogQueryResult(
            match_count=1,
            events=(
                LogEvent(
                    timestamp=NOW,
                    service="order-service",
                    level="error",
                    message="order handler rejected a missing required configuration value",
                    error_type="MissingRequiredConfiguration",
                ),
            ),
            provenance=AdapterProvenance(source="deterministic-acceptance", observed_at=NOW),
        )


class _TimeoutLogsAdapter:
    """Raises a provider-shaped error whose raw text must not enter workflow state."""

    def __init__(self) -> None:
        self.calls = 0

    def query(self, _: object) -> LogQueryResult:
        self.calls += 1
        raise AdapterError(
            "timeout",
            "raw-provider-secret=must-not-persist",
            retryable=True,
        )


class _ContinueEvaluator:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, _: AgentState) -> EvaluationDecision:
        self.calls += 1
        return EvaluationDecision.CONTINUE


def _started_incident(session: Session) -> tuple[Incident, InvestigationRound, Service]:
    target = InvestigationTarget(
        name="M4 workflow reliability target",
        slug=f"m4-reliability-{uuid4().hex}",
        environment="local",
        enabled=True,
    )
    service = Service(
        target=target,
        name="order-service",
        display_name="Order service",
        enabled=True,
    )
    session.add(target)
    session.flush()
    incident = Incident(
        target_id=target.id,
        service_id=service.id,
        service=service.name,
        environment="local",
        description="订单接口在当前窗口持续返回 500。",
        time_range_start=NOW - timedelta(minutes=5),
        time_range_end=NOW,
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    round_record = session.scalar(
        select(InvestigationRound).where(InvestigationRound.incident_id == incident.id)
    )
    assert round_record is not None
    InvestigationLifecycleService(session).start(round_record.id)
    return incident, round_record, service


def _state(incident: Incident, round_record: InvestigationRound) -> AgentState:
    state = create_initial_agent_state(incident)
    state["round_id"] = round_record.id
    return state


def _dependencies(
    rag_service: _DeterministicRAG,
    llm_client: _DeterministicLLM,
    logs_adapter: _NormalLogsAdapter | _TimeoutLogsAdapter,
    evaluator: _ContinueEvaluator,
) -> V2InvestigationWorkflowDependencies:
    return V2InvestigationWorkflowDependencies(
        rag_service=rag_service,  # type: ignore[arg-type]
        llm_client=llm_client,
        tool_execution=ToolExecutionDependencies(  # type: ignore[arg-type]
            rag_service=rag_service,  # type: ignore[arg-type]
            logs_adapter=logs_adapter,
            metrics_adapter=None,
            traces_adapter=None,
            deployment_adapter=None,
            available_tools=frozenset({ToolName.QUERY_LOGS}),
        ),
        evaluator=evaluator,
    )


def _report(session: Session, round_record: InvestigationRound) -> Report:
    report = session.scalar(select(Report).where(Report.round_id == round_record.id))
    assert report is not None
    return report


def test_successful_workflow_acceptance_resumes_without_losing_grounded_state(
    database_session: Session,
    monkeypatch,
) -> None:
    """Formal graph reaches a citation-backed conclusion after a checkpoint interruption."""
    incident, round_record, service_record = _started_incident(database_session)
    rag_service = _DeterministicRAG(
        target_id=incident.target_id,
        service_id=service_record.id,
    )
    llm_client = _DeterministicLLM(confirm=True)
    logs_adapter = _NormalLogsAdapter()
    observer = _RecordingObserver()
    pause_once = True
    original_boundary = workflow_module._v2_investigation_node

    def pause_before_update(node_name: str, *args: object):
        bounded = original_boundary(node_name, *args)  # type: ignore[arg-type]
        if node_name != "hypothesis_update":
            return bounded

        def interrupted_node(state: AgentState) -> AgentState:
            nonlocal pause_once
            if pause_once:
                pause_once = False
                interrupt("pause before grounded hypothesis update")
            return bounded(state)

        return interrupted_node

    monkeypatch.setattr(workflow_module, "_v2_investigation_node", pause_before_update)
    graph = build_v2_production_investigation_graph(
        _dependencies(rag_service, llm_client, logs_adapter, _ContinueEvaluator()),
        session=database_session,
        checkpointer=InMemorySaver(),
        observer=observer,
    )
    runtime = WorkflowService(graph)

    interrupted = runtime.start(incident, round_id=round_record.id)
    paused = runtime.get_state(incident.thread_id)

    assert "__interrupt__" in interrupted
    assert paused["current_stage"] is AgentStage.HYPOTHESIS_UPDATE
    assert paused["investigation_round"] == 0
    assert paused["tool_call_count"] == 2
    assert paused["retry_count"] == 0
    assert {item.source for item in paused["evidence"]} == {
        "search_knowledge",
        "query_logs",
    }
    paused_evidence_ids = {item.id for item in paused["evidence"]}
    paused_hypothesis_ids = {item.id for item in paused["hypotheses"]}

    result = runtime.resume(incident.thread_id, {"continue": True})

    database_session.refresh(incident)
    database_session.refresh(round_record)
    report = _report(database_session, round_record)
    assert incident.investigation_status is InvestigationStatus.CONCLUDED
    assert round_record.status is InvestigationStatus.CONCLUDED
    assert result["terminal_reason"] is TerminalReason.SUFFICIENT_EVIDENCE
    assert result["investigation_round"] == 1
    assert result["tool_call_count"] == paused["tool_call_count"]
    assert result["retry_count"] == paused["retry_count"]
    assert paused_evidence_ids.issubset({item.id for item in result["evidence"]})
    assert paused_hypothesis_ids == {item.id for item in result["hypotheses"]}
    assert logs_adapter.calls == 1
    assert rag_service.scopes == [
        KnowledgeScope(
            target_id=incident.target_id,
            service_id=service_record.id,
            environment="local",
        )
    ]
    assert {
        "intake",
        "retrieval",
        "hypothesis_generation",
        "investigation_planning",
        "tool_execution",
        "hypothesis_update",
        "evidence_evaluation",
        "conclusion",
        "conclusion_terminalization",
    }.issubset(observer.started)

    conclusion = result["final_conclusion"]
    assert conclusion is not None
    evidence_by_id = {item.id: item for item in result["evidence"]}
    assert set(conclusion.supporting_evidence_ids).issubset(evidence_by_id)
    assert all(item.round_id == round_record.id for item in evidence_by_id.values())
    assert any(item.source == "query_logs" for item in evidence_by_id.values())
    for evidence_id in conclusion.supporting_evidence_ids:
        evidence = evidence_by_id[evidence_id]
        if evidence.source == "search_knowledge":
            assert evidence.citation is not None
    assert report.content["final_status"] == InvestigationStatus.CONCLUDED.value
    assert report.content["terminal_reason"] == TerminalReason.SUFFICIENT_EVIDENCE.value
    assert report.content["conclusion"]["root_cause"] == conclusion.root_cause
    assert report.content["conclusion"]["citations"]


def test_insufficient_evidence_acceptance_reports_uncertainty_without_root_cause(
    database_session: Session,
) -> None:
    """A normal Tool result that cannot ground confirmation reaches INCONCLUSIVE."""
    incident, round_record, service_record = _started_incident(database_session)
    rag_service = _DeterministicRAG(
        target_id=incident.target_id,
        service_id=service_record.id,
    )
    logs_adapter = _NormalLogsAdapter()
    evaluator = _ContinueEvaluator()
    result = build_v2_production_investigation_graph(
        _dependencies(rag_service, _DeterministicLLM(confirm=False), logs_adapter, evaluator),
        session=database_session,
        budget=InvestigationBudget(max_iterations=1, max_tool_calls=3),
    ).invoke(_state(incident, round_record))

    database_session.refresh(incident)
    database_session.refresh(round_record)
    report = _report(database_session, round_record)
    assert incident.investigation_status is InvestigationStatus.INCONCLUSIVE
    assert round_record.status is InvestigationStatus.INCONCLUSIVE
    assert result["terminal_reason"] is TerminalReason.ITERATION_BUDGET_EXHAUSTED
    assert result["final_conclusion"] is None
    assert all(item.status is not HypothesisStatus.CONFIRMED for item in result["hypotheses"])
    assert logs_adapter.calls == 1
    assert evaluator.calls == 1
    assert {item.source for item in result["evidence"]} == {
        "search_knowledge",
        "query_logs",
    }
    assert report.content["conclusion"] is None
    assert report.content["unknowns"]
    assert report.content["key_evidence"]
    assert report.root_cause is None


def test_repeated_runtime_failure_acceptance_is_bounded_and_keeps_only_safe_facts(
    database_session: Session,
) -> None:
    """Retryable provider timeouts end FAILED without creating runtime Evidence or leaking text."""
    incident, round_record, service_record = _started_incident(database_session)
    rag_service = _DeterministicRAG(
        target_id=incident.target_id,
        service_id=service_record.id,
    )
    logs_adapter = _TimeoutLogsAdapter()
    result = build_v2_production_investigation_graph(
        _dependencies(
            rag_service,
            _DeterministicLLM(confirm=True),
            logs_adapter,
            _ContinueEvaluator(),
        ),
        session=database_session,
        budget=InvestigationBudget(
            max_tool_calls=4,
            max_consecutive_failures=3,
            max_workflow_retries=1,
        ),
    ).invoke(_state(incident, round_record))

    database_session.refresh(incident)
    database_session.refresh(round_record)
    report = _report(database_session, round_record)
    tool_calls = list(
        database_session.scalars(
            select(ToolCall)
            .where(ToolCall.round_id == round_record.id)
            .order_by(ToolCall.created_at)
        )
    )
    assert incident.investigation_status is InvestigationStatus.FAILED
    assert round_record.status is InvestigationStatus.FAILED
    assert result["terminal_reason"] is TerminalReason.RETRY_BUDGET_EXHAUSTED
    assert logs_adapter.calls == 2
    assert result["tool_call_count"] == 3
    assert result["retry_count"] == 1
    assert result["retry_pending"] is False
    assert result["consecutive_failures"] == 2
    assert all(item.source != "query_logs" for item in result["evidence"])
    assert [item.status for item in result["tool_history"][-2:]] == [
        ToolStatus.FAILURE,
        ToolStatus.FAILURE,
    ]
    assert all(
        item.error is not None and item.error.code == "timeout"
        for item in result["tool_history"][-2:]
    )
    assert all(item.error == "The runtime evidence provider timed out." for item in tool_calls[-2:])
    assert "raw-provider-secret" not in str(result)
    assert "raw-provider-secret" not in str(report.content)
    assert all("raw-provider-secret" not in (item.error or "") for item in tool_calls)
    assert report.content["conclusion"] is None
    assert report.content["final_status"] == InvestigationStatus.FAILED.value
