"""Regression coverage for the formal V2 read-only runtime boundary."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import devsupport_backend.agent.workflow as workflow_module
import devsupport_backend.workflow_console as workflow_console_module
from devsupport_backend.agent.nodes.tool_execution import ToolExecutionDependencies
from devsupport_backend.agent.state import (
    AgentStage,
    EvidenceContext,
    FinalConclusion,
    HypothesisContext,
    HypothesisStatus,
    TerminalReason,
    create_initial_agent_state,
)
from devsupport_backend.agent.v2_terminalization import V2Terminalizer
from devsupport_backend.agent.workflow import (
    InvestigationLoopLimits,
    V2InvestigationWorkflowDependencies,
    build_v2_production_investigation_graph,
)
from devsupport_backend.investigation_lifecycle import InvestigationLifecycleService
from devsupport_backend.investigation_status import InvestigationStatus
from devsupport_backend.main import app
from devsupport_backend.models import (
    Action,
    Approval,
    Evidence,
    Incident,
    InvestigationRound,
    Report,
    Verification,
)
from devsupport_backend.tools.registry import (
    V2_READ_ONLY_TOOL_NAMES,
    ToolName,
    UnknownToolError,
    v2_tool_registry,
)
from devsupport_backend.tools.schemas import CitationOutput
from devsupport_backend.workflow_console import PostgresWorkflowRuntime


class _UnusedLLM:
    def complete(self, **_: object) -> str:
        raise AssertionError("this graph-shape test must not call the LLM")


class _UnusedEvaluator:
    def evaluate(self, _: object) -> object:
        raise AssertionError("this graph-shape test must not evaluate evidence")


def _incident(session: Session) -> tuple[Incident, InvestigationRound]:
    now = datetime.now(UTC)
    incident = Incident(
        service="order-service",
        environment="local",
        description="订单接口持续返回 500。",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=5),
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    round_record = session.scalar(
        select(InvestigationRound).where(
            InvestigationRound.incident_id == incident.id,
            InvestigationRound.thread_id == incident.thread_id,
        )
    )
    assert round_record is not None
    InvestigationLifecycleService(session).start(round_record.id)
    return incident, round_record


def _state(incident: Incident, round_record: InvestigationRound) -> dict[str, object]:
    state = create_initial_agent_state(incident)
    state["round_id"] = round_record.id
    return state


def test_v2_graph_has_only_investigation_and_terminal_report_nodes(
    database_session: Session,
) -> None:
    dependencies = V2InvestigationWorkflowDependencies(
        rag_service=object(),  # type: ignore[arg-type]
        llm_client=_UnusedLLM(),  # type: ignore[arg-type]
        tool_execution=SimpleNamespace(available_tools=V2_READ_ONLY_TOOL_NAMES),  # type: ignore[arg-type]
        evaluator=_UnusedEvaluator(),  # type: ignore[arg-type]
    )

    graph = build_v2_production_investigation_graph(
        dependencies,
        session=database_session,
        limits=InvestigationLoopLimits(max_rounds=1, max_tool_calls=1),
    )

    nodes = set(graph.get_graph().nodes)
    assert {
        "intake",
        "retrieval",
        "hypothesis_generation",
        "planning_guard",
        "investigation_planning",
        "tool_execution",
        "hypothesis_update",
        "evidence_evaluation",
        "conclusion",
        "conclusion_terminalization",
        "inconclusive_terminalization",
        "failure_terminalization",
    } <= nodes
    assert not nodes.intersection(
        {
            "resolution_proposal",
            "policy_gate",
            "approval_wait",
            "approval_interrupt",
            "approval_decision",
            "controlled_action_execution",
            "recovery_verification",
        }
    )
    assert set(V2InvestigationWorkflowDependencies.__dataclass_fields__) == {
        "rag_service",
        "llm_client",
        "tool_execution",
        "evaluator",
    }


def test_postgres_runtime_composes_only_v2_investigation_dependencies(
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Factory:
        @classmethod
        def from_settings(cls, *_: object) -> object:
            return object()

    captured: dict[str, object] = {}
    sentinel = object()
    monkeypatch.setattr(workflow_console_module, "OpenAICompatibleLLMClient", _Factory)
    monkeypatch.setattr(workflow_console_module, "OpenAICompatibleEmbeddingClient", _Factory)
    monkeypatch.setattr(workflow_console_module, "RAGService", lambda *_: object())
    monkeypatch.setattr(
        PostgresWorkflowRuntime,
        "_tool_execution_dependencies",
        staticmethod(lambda *_: SimpleNamespace(available_tools=V2_READ_ONLY_TOOL_NAMES)),
    )

    def capture_dependencies(
        dependencies: V2InvestigationWorkflowDependencies, **_: object
    ) -> object:
        captured["dependencies"] = dependencies
        return sentinel

    monkeypatch.setattr(
        workflow_console_module, "build_v2_production_investigation_graph", capture_dependencies
    )

    result = PostgresWorkflowRuntime(database_session)._production_graph(object(), object())

    assert result is sentinel
    assert isinstance(captured["dependencies"], V2InvestigationWorkflowDependencies)


def test_v2_tool_registry_excludes_side_effect_tools() -> None:
    assert ToolName.ROLLBACK_DEPLOYMENT not in V2_READ_ONLY_TOOL_NAMES
    assert {definition.name for definition in v2_tool_registry.list()} == V2_READ_ONLY_TOOL_NAMES
    with pytest.raises(UnknownToolError):
        v2_tool_registry.get(ToolName.ROLLBACK_DEPLOYMENT)
    with pytest.raises(ValueError, match="read-only"):
        ToolExecutionDependencies(  # type: ignore[arg-type]
            rag_service=object(),
            logs_adapter=object(),
            metrics_adapter=object(),
            traces_adapter=None,
            deployment_adapter=None,
            available_tools=frozenset({ToolName.ROLLBACK_DEPLOYMENT}),
        )


def test_v2_graph_concludes_and_terminalizes_the_current_round(
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incident, round_record = _incident(database_session)
    evidence = EvidenceContext(
        source="query_logs",
        evidence_type="log_query_result",
        summary="依赖连接被拒绝。",
        data={"match_count": 3},
    )
    hypothesis = HypothesisContext(
        summary="订单服务无法连接依赖服务。",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.9,
        supporting_evidence_ids=[evidence.id],
    )

    monkeypatch.setattr(
        workflow_module,
        "intake_node",
        lambda state: {**state, "current_stage": AgentStage.RETRIEVAL},
    )
    monkeypatch.setattr(
        workflow_module,
        "retrieval_node",
        lambda state, _: {**state, "current_stage": AgentStage.HYPOTHESIS_GENERATION},
    )
    monkeypatch.setattr(
        workflow_module,
        "hypothesis_generation_node",
        lambda state, _: {**state, "current_stage": AgentStage.INVESTIGATION_PLANNING},
    )
    monkeypatch.setattr(
        workflow_module,
        "_investigation_planning_node",
        lambda state, *_: {**state, "current_stage": AgentStage.TOOL_EXECUTION},
    )
    monkeypatch.setattr(
        workflow_module,
        "_tool_execution_with_initial_evidence_batch",
        lambda state, _: {**state, "current_stage": AgentStage.HYPOTHESIS_UPDATE},
    )
    monkeypatch.setattr(
        workflow_module,
        "_hypothesis_update_round_node",
        lambda state, _: {
            **state,
            "current_stage": AgentStage.EVIDENCE_EVALUATION,
            "evidence": [evidence],
            "hypotheses": [hypothesis],
        },
    )
    dependencies = V2InvestigationWorkflowDependencies(
        rag_service=object(),  # type: ignore[arg-type]
        llm_client=_UnusedLLM(),  # type: ignore[arg-type]
        tool_execution=SimpleNamespace(available_tools=V2_READ_ONLY_TOOL_NAMES),  # type: ignore[arg-type]
        evaluator=_UnusedEvaluator(),  # type: ignore[arg-type]
    )

    result = build_v2_production_investigation_graph(
        dependencies, session=database_session
    ).invoke(_state(incident, round_record))

    database_session.refresh(incident)
    database_session.refresh(round_record)
    assert result["report_outcome"] is not None
    assert incident.investigation_status is InvestigationStatus.CONCLUDED
    assert round_record.status is InvestigationStatus.CONCLUDED


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        ("inconclusive", InvestigationStatus.INCONCLUSIVE),
        ("failure", InvestigationStatus.FAILED),
    ],
)
def test_v2_graph_terminalizes_bounded_and_controlled_failures(
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    status: InvestigationStatus,
) -> None:
    incident, round_record = _incident(database_session)
    if mode == "inconclusive":
        monkeypatch.setattr(
            workflow_module,
            "intake_node",
            lambda state: {
                **state,
                "evaluation_decision": workflow_module.EvaluationDecision.NEEDS_MANUAL_ACTION,
                "terminal_reason": TerminalReason.INVESTIGATION_INCONCLUSIVE,
            },
        )
    else:
        monkeypatch.setattr(
            workflow_module,
            "intake_node",
            lambda _: (_ for _ in ()).throw(RuntimeError("controlled failure")),
        )
    dependencies = V2InvestigationWorkflowDependencies(
        rag_service=object(),  # type: ignore[arg-type]
        llm_client=_UnusedLLM(),  # type: ignore[arg-type]
        tool_execution=object(),  # type: ignore[arg-type]
        evaluator=_UnusedEvaluator(),  # type: ignore[arg-type]
    )

    result = build_v2_production_investigation_graph(
        dependencies, session=database_session
    ).invoke(_state(incident, round_record))

    database_session.refresh(incident)
    database_session.refresh(round_record)
    assert result["report_outcome"] is not None
    assert incident.investigation_status is status
    assert round_record.status is status


def test_v2_conclusion_terminalization_creates_report_without_remediation(
    database_session: Session,
) -> None:
    incident, round_record = _incident(database_session)
    state = _state(incident, round_record)
    evidence = EvidenceContext(
        source="query_metrics",
        evidence_type="metric_snapshot",
        summary="错误率持续升高。",
        data={"error_rate": 0.5},
    )
    hypothesis = HypothesisContext(
        summary="订单服务的依赖调用失败。",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.9,
        supporting_evidence_ids=[evidence.id],
    )
    state.update(
        {
            "current_stage": AgentStage.CONCLUSION,
            "evidence": [evidence],
            "hypotheses": [hypothesis],
            "final_conclusion": FinalConclusion(
                summary="已确认依赖调用失败。",
                root_cause=hypothesis.summary,
                confidence=0.9,
                supporting_evidence_ids=[evidence.id],
                recommended_next_action="请人工检查依赖服务。",
            ),
        }
    )

    result = V2Terminalizer(database_session).terminalize(
        state, InvestigationStatus.CONCLUDED
    )

    database_session.refresh(incident)
    database_session.refresh(round_record)
    report = database_session.scalar(select(Report).where(Report.round_id == round_record.id))
    assert incident.investigation_status is InvestigationStatus.CONCLUDED
    assert round_record.status is InvestigationStatus.CONCLUDED
    assert result["report_outcome"] is not None
    assert report is not None
    assert report.content["schema_version"] == "v2"
    assert not {"action", "approval", "execution", "verification"}.intersection(report.content)
    assert "恢复" not in str(report.content)
    assert database_session.scalar(select(Action).where(Action.incident_id == incident.id)) is None
    assert (
        database_session.scalar(select(Approval).where(Approval.incident_id == incident.id))
        is None
    )
    assert (
        database_session.scalar(select(Verification).where(Verification.incident_id == incident.id))
        is None
    )


def test_v2_knowledge_evidence_binds_hypothesis_conclusion_and_report_citations(
    database_session: Session,
) -> None:
    incident, round_record = _incident(database_session)
    state = _state(incident, round_record)
    citation = CitationOutput(
        id=f"knowledge:{uuid4()}:{uuid4()}",
        document_id=uuid4(),
        chunk_id=uuid4(),
        document_title="订单服务故障排查手册",
        source="knowledge/runbooks/order-errors.md",
        source_path="knowledge/runbooks/order-errors.md",
        chunk_index=3,
        section="依赖检查",
        document_version="2026.09",
        target_id=incident.target_id,
        scope="service",
        service_id=incident.service_id,
        environment="local",
        document_reference="knowledge/runbooks/order-errors.md#chunk-3",
    )
    evidence = EvidenceContext(
        source="search_knowledge",
        evidence_type="knowledge_retrieval",
        summary="手册要求先检查订单服务到依赖服务的连接失败。",
        citation=citation,
    )
    hypothesis = HypothesisContext(
        summary="订单服务的依赖调用失败。",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.9,
        supporting_evidence_ids=[evidence.id],
    )
    state.update(
        {
            "evidence": [evidence],
            "hypotheses": [hypothesis],
            "final_conclusion": FinalConclusion(
                summary="知识证据支持优先检查依赖调用。",
                root_cause=hypothesis.summary,
                confidence=0.9,
                supporting_evidence_ids=[evidence.id],
            ),
        }
    )

    V2Terminalizer(database_session).terminalize(state, InvestigationStatus.CONCLUDED)

    persisted_evidence = database_session.scalar(
        select(Evidence).where(Evidence.round_id == round_record.id)
    )
    report = database_session.scalar(select(Report).where(Report.round_id == round_record.id))
    assert persisted_evidence is not None
    assert persisted_evidence.data["citation"] == citation.model_dump(mode="json")
    assert report is not None
    assert report.content["key_evidence"][0]["citation"] == citation.model_dump(mode="json")
    assert report.content["conclusion"]["citations"] == [citation.model_dump(mode="json")]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (InvestigationStatus.INCONCLUSIVE, TerminalReason.INVESTIGATION_INCONCLUSIVE),
        (InvestigationStatus.FAILED, TerminalReason.WORKFLOW_FAILURE),
    ],
)
def test_v2_terminalization_synchronizes_nonconclusive_statuses(
    database_session: Session,
    status: InvestigationStatus,
    reason: TerminalReason,
) -> None:
    incident, round_record = _incident(database_session)
    state = _state(incident, round_record)
    state.update({"terminal_reason": reason})

    V2Terminalizer(database_session).terminalize(state, status)

    database_session.refresh(incident)
    database_session.refresh(round_record)
    report = database_session.scalar(select(Report).where(Report.round_id == round_record.id))
    assert incident.investigation_status is status
    assert round_record.status is status
    assert report is not None
    assert report.content["final_status"] == status.value
    assert report.content["terminal_reason"] == reason.value


def test_formal_v2_incident_api_has_no_remediation_routes() -> None:
    with TestClient(app) as client:
        incident_approval = client.post(
            f"/incidents/{uuid4()}/approval", json={"decision": "APPROVE"}
        )
        legacy_approval = client.post(
            f"/legacy/incidents/{uuid4()}/approval", json={"decision": "APPROVE"}
        )
        paths = client.get("/openapi.json").json()["paths"]

    assert incident_approval.status_code == 404
    assert legacy_approval.status_code == 404
    assert not any(
        term in path for path in paths for term in ("approval", "legacy", "remediation", "recovery")
    )
