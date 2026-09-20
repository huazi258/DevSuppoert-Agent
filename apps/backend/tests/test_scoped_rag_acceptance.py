"""Deterministic acceptance coverage for formal V2 scoped RAG isolation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

import devsupport_backend.agent.nodes.retrieval as retrieval_module
from devsupport_backend.agent.nodes.retrieval import retrieval_node
from devsupport_backend.agent.state import (
    AgentStage,
    FinalConclusion,
    HypothesisContext,
    HypothesisStatus,
    IntakeDecision,
    TerminalReason,
    create_initial_agent_state,
)
from devsupport_backend.agent.v2_terminalization import V2Terminalizer
from devsupport_backend.investigation_lifecycle import InvestigationLifecycleService
from devsupport_backend.investigation_status import InvestigationStatus
from devsupport_backend.models import (
    MIGRATION_COMPATIBILITY_TARGET_SLUG,
    Incident,
    InvestigationRound,
    InvestigationTarget,
    KnowledgeChunk,
    KnowledgeDocument,
    Report,
    Service,
)
from devsupport_backend.rag.retrieval import KnowledgeScope, RAGService
from devsupport_backend.tools.schemas import SearchKnowledgeInput
from devsupport_backend.tools.search_knowledge import search_knowledge as real_search_knowledge


class _EmbeddingClient:
    def embed(self, _: list[str]) -> list[list[float]]:
        return [[1.0, 0.0]]


@dataclass(frozen=True)
class _TargetServices:
    target: InvestigationTarget
    order: Service
    payment: Service


def _target_services(session: Session, slug: str) -> _TargetServices:
    target = InvestigationTarget(
        name=f"{slug} target",
        slug=slug,
        environment="local",
        enabled=True,
    )
    order = Service(
        target=target,
        name="order-service",
        display_name="Order service",
        enabled=True,
    )
    payment = Service(
        target=target,
        name="payment-service",
        display_name="Payment service",
        enabled=True,
    )
    session.add(target)
    session.flush()
    return _TargetServices(target=target, order=order, payment=payment)


def _add_document(
    session: Session,
    *,
    title: str,
    target_id: UUID,
    scope: str,
    service: Service | None,
    environment: str,
    status: str = "enabled",
    content: str,
    embedding: list[float],
) -> KnowledgeChunk:
    document = KnowledgeDocument(
        title=title,
        source_path=f"knowledge/acceptance/{uuid4()}.md",
        content_hash="a" * 64,
        target_id=target_id,
        scope=scope,
        service_id=service.id if service else None,
        service=service.name if service else None,
        environment=environment,
        document_type="runbook",
        version="2026.09",
        status=status,
        metadata_data={},
    )
    chunk = KnowledgeChunk(
        chunk_index=0,
        content=content,
        embedding=embedding,
        metadata_data={"section": "Investigation"},
    )
    document.chunks.append(chunk)
    session.add(document)
    session.flush()
    return chunk


def _started_incident(
    session: Session, target: InvestigationTarget, service: Service, *, description: str
) -> tuple[Incident, InvestigationRound]:
    started_at = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
    incident = Incident(
        target_id=target.id,
        service_id=service.id,
        service=service.name,
        environment="local",
        description=description,
        time_range_start=started_at,
        time_range_end=started_at + timedelta(minutes=5),
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    round_record = session.scalar(
        select(InvestigationRound).where(InvestigationRound.incident_id == incident.id)
    )
    assert round_record is not None
    InvestigationLifecycleService(session).start(round_record.id)
    return incident, round_record


def _ready_state(incident: Incident, round_record: InvestigationRound) -> dict[str, object]:
    state = create_initial_agent_state(incident, symptoms=["payment gateway timeout"])
    state.update(
        {
            "round_id": round_record.id,
            "intake_decision": IntakeDecision.READY,
            "current_stage": AgentStage.RETRIEVAL,
        }
    )
    return state


def test_formal_v2_scoped_rag_acceptance_preserves_only_authorized_citations(
    database_session: Session, monkeypatch
) -> None:
    target_a = _target_services(database_session, f"acceptance-a-{uuid4()}")
    target_b = _target_services(database_session, f"acceptance-b-{uuid4()}")
    compatibility_target = database_session.scalar(
        select(InvestigationTarget).where(
            InvestigationTarget.slug == MIGRATION_COMPATIBILITY_TARGET_SLUG
        )
    )
    assert compatibility_target is not None
    query = "payment gateway timeout"
    shared = _add_document(
        database_session,
        title="Target A shared timeout runbook",
        target_id=target_a.target.id,
        scope="shared",
        service=None,
        environment="common",
        content=f"{query} authorized shared investigation",
        embedding=[0.6, 0.8],
    )
    service_document = _add_document(
        database_session,
        title="Target A order timeout runbook",
        target_id=target_a.target.id,
        scope="service",
        service=target_a.order,
        environment="local",
        content=f"{query} authorized order-service investigation",
        embedding=[0.5, 0.8],
    )
    interference_content = f"{query} {query} {query} {query} {query}"
    forbidden = {
        _add_document(
            database_session,
            title="Target A payment interference",
            target_id=target_a.target.id,
            scope="service",
            service=target_a.payment,
            environment="local",
            content=interference_content,
            embedding=[1.0, 0.0],
        ).id,
        _add_document(
            database_session,
            title="Target B interference",
            target_id=target_b.target.id,
            scope="shared",
            service=None,
            environment="common",
            content=interference_content,
            embedding=[1.0, 0.0],
        ).id,
        _add_document(
            database_session,
            title="Target A staging interference",
            target_id=target_a.target.id,
            scope="service",
            service=target_a.order,
            environment="staging",
            content=interference_content,
            embedding=[1.0, 0.0],
        ).id,
        _add_document(
            database_session,
            title="Target A disabled interference",
            target_id=target_a.target.id,
            scope="shared",
            service=None,
            environment="common",
            status="disabled",
            content=interference_content,
            embedding=[1.0, 0.0],
        ).id,
        _add_document(
            database_session,
            title="Compatibility interference",
            target_id=compatibility_target.id,
            scope="shared",
            service=None,
            environment="common",
            content=interference_content,
            embedding=[1.0, 0.0],
        ).id,
    }
    database_session.commit()
    incident, round_record = _started_incident(
        database_session, target_a.target, target_a.order, description=query
    )
    state = _ready_state(incident, round_record)
    rag_service = RAGService(database_session, _EmbeddingClient())
    captured_inputs: list[SearchKnowledgeInput] = []
    captured_scopes: list[KnowledgeScope] = []
    real_search_scoped = rag_service.search_scoped

    def capture_scoped(
        query: str,
        *,
        scope: KnowledgeScope,
        document_type: str | None = None,
        top_k: int = 5,
    ):
        captured_scopes.append(scope)
        return real_search_scoped(
            query, scope=scope, document_type=document_type, top_k=top_k
        )

    def capture_tool(
        tool_input: SearchKnowledgeInput, service: RAGService
    ):
        captured_inputs.append(tool_input)
        return real_search_knowledge(tool_input, service)

    monkeypatch.setattr(rag_service, "search_scoped", capture_scoped)
    monkeypatch.setattr(retrieval_module, "search_knowledge", capture_tool)

    retrieved = retrieval_node(state, rag_service)

    assert captured_inputs == [
        SearchKnowledgeInput(
            query=query,
            target_id=target_a.target.id,
            service_id=target_a.order.id,
            environment="local",
        )
    ]
    assert captured_scopes == [
        KnowledgeScope(
            target_id=target_a.target.id,
            service_id=target_a.order.id,
            environment="local",
        )
    ]
    assert retrieved["current_stage"] is AgentStage.HYPOTHESIS_GENERATION
    assert {UUID(item.data["chunk_id"]) for item in retrieved["evidence"]} == {
        shared.id,
        service_document.id,
    }
    assert forbidden.isdisjoint(UUID(item.data["chunk_id"]) for item in retrieved["evidence"])
    assert all(item.citation is not None for item in retrieved["evidence"])
    assert all(item.citation.target_id == target_a.target.id for item in retrieved["evidence"])
    assert {
        (item.citation.scope, item.citation.service_id, item.citation.environment)
        for item in retrieved["evidence"]
        if item.citation is not None
    } == {
        ("shared", None, "common"),
        ("service", target_a.order.id, "local"),
    }
    hypothesis = HypothesisContext(
        summary="Target A 的订单服务应优先检查支付网关超时。",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.8,
        supporting_evidence_ids=[item.id for item in retrieved["evidence"]],
    )
    concluded = {
        **retrieved,
        "hypotheses": [hypothesis],
        "final_conclusion": FinalConclusion(
            summary="仅当前 Target/Service 的知识支持支付网关超时排查。",
            root_cause=hypothesis.summary,
            confidence=0.8,
            supporting_evidence_ids=[item.id for item in retrieved["evidence"]],
        ),
    }

    V2Terminalizer(database_session).terminalize(concluded, InvestigationStatus.CONCLUDED)

    report = database_session.scalar(select(Report).where(Report.round_id == round_record.id))
    assert report is not None
    report_citations = [
        *[item["citation"] for item in report.content["key_evidence"]],
        *report.content["conclusion"]["citations"],
    ]
    assert len(report_citations) == 4
    assert all(item["target_id"] == str(target_a.target.id) for item in report_citations)
    assert all(item["environment"] in {"common", "local"} for item in report_citations)
    assert all(
        item["service_id"] in {None, str(target_a.order.id)} for item in report_citations
    )
    retrieved_chunk_ids = {UUID(item.data["chunk_id"]) for item in retrieved["evidence"]}
    invalid_report_citations = [
        item
        for item in report_citations
        if item["target_id"] != str(target_a.target.id)
        or item["environment"] not in {"common", "local"}
        or item["service_id"] not in {None, str(target_a.order.id)}
    ]
    print(
        "V2_RELEASE_FACT="
        + json.dumps(
            {
                "scope_leakage_count": len(retrieved_chunk_ids & forbidden)
                + len(invalid_report_citations)
            }
        )
    )


def test_formal_v2_scoped_rag_empty_result_remains_uncited_and_inconclusive(
    database_session: Session,
) -> None:
    target = _target_services(database_session, f"empty-{uuid4()}")
    incident, round_record = _started_incident(
        database_session,
        target.target,
        target.order,
        description="unobserved dependency timeout",
    )
    state = _ready_state(incident, round_record)

    retrieved = retrieval_node(state, RAGService(database_session, _EmbeddingClient()))

    assert retrieved["evidence"] == []
    assert retrieved["final_conclusion"] is None
    assert retrieved["tool_history"][0].evidence_ids == []
    terminal = {
        **retrieved,
        "terminal_reason": TerminalReason.INVESTIGATION_INCONCLUSIVE,
    }
    V2Terminalizer(database_session).terminalize(terminal, InvestigationStatus.INCONCLUSIVE)

    report = database_session.scalar(select(Report).where(Report.round_id == round_record.id))
    assert report is not None
    assert report.content["conclusion"] is None
    assert report.content["key_evidence"] == []
