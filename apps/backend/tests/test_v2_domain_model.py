"""Persistence contracts for the M1.1 V2 domain skeleton."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from devsupport_backend.models import (
    Evidence,
    Hypothesis,
    Incident,
    InvestigationRound,
    InvestigationTarget,
    Observation,
    Report,
    Service,
    ToolCall,
)


def _target_with_service() -> tuple[InvestigationTarget, Service]:
    target = InvestigationTarget(
        name="订单系统测试环境",
        slug=f"order-test-{uuid4()}",
        description="用于领域模型测试的调查目标。",
        environment="test",
        enabled=True,
    )
    service = Service(
        name="order-service",
        display_name="订单服务",
        description="处理订单请求。",
        enabled=True,
    )
    target.services.append(service)
    return target, service


def _incident(target: InvestigationTarget, service: Service) -> Incident:
    return Incident(
        service="order-service",
        environment="test",
        description="创建订单时返回 500。",
        time_range_start=datetime(2026, 9, 19, 8, tzinfo=UTC),
        time_range_end=datetime(2026, 9, 19, 9, tzinfo=UTC),
        status="OPEN",
        thread_id=str(uuid4()),
        target=target,
        service_record=service,
    )


def test_target_service_and_incident_relationships(database_session: Session) -> None:
    target, service = _target_with_service()
    incident = _incident(target, service)
    database_session.add(incident)
    database_session.commit()

    assert incident.target is target
    assert incident.service_record is service
    assert service.target is target
    assert incident.target_id == target.id
    assert incident.service_id == service.id
    assert len(incident.rounds) == 1
    assert incident.rounds[0].thread_id == incident.thread_id


def test_service_name_is_unique_within_a_target(database_session: Session) -> None:
    target, service = _target_with_service()
    database_session.add(target)
    database_session.commit()
    database_session.add(
        Service(target_id=target.id, name=service.name, display_name="重复服务", enabled=True)
    )

    with pytest.raises(IntegrityError):
        database_session.commit()
    database_session.rollback()


def test_incident_cannot_bind_a_service_from_another_target(database_session: Session) -> None:
    target_a, _ = _target_with_service()
    target_b, service_b = _target_with_service()
    database_session.add_all([target_a, target_b])
    database_session.commit()
    incident = Incident(
        service="order-service",
        environment="test",
        description="错误绑定到另一个 Target 的 Service。",
        time_range_start=datetime(2026, 9, 19, 8, tzinfo=UTC),
        time_range_end=datetime(2026, 9, 19, 9, tzinfo=UTC),
        status="OPEN",
        thread_id=str(uuid4()),
        target_id=target_a.id,
        service_id=service_b.id,
    )
    database_session.add(incident)

    with pytest.raises(IntegrityError):
        database_session.commit()
    database_session.rollback()


def _two_incidents(database_session: Session) -> tuple[Incident, Incident, InvestigationRound]:
    target, service = _target_with_service()
    second_service = Service(name="payment-service", display_name="支付服务", enabled=True)
    target.services.append(second_service)
    first_incident = _incident(target, service)
    second_incident = _incident(target, second_service)
    database_session.add_all([first_incident, second_incident])
    database_session.commit()
    return first_incident, second_incident, second_incident.rounds[0]


@pytest.mark.parametrize(
    "record_factory",
    [
        lambda incident, round_record: Observation(
            incident_id=incident.id,
            round_id=round_record.id,
            content="交叉 Incident 的观察。",
            observed_at=datetime(2026, 9, 19, 10, tzinfo=UTC),
        ),
        lambda incident, round_record: Hypothesis(
            incident_id=incident.id,
            round_id=round_record.id,
            summary="交叉 Incident 的假设。",
            status="OPEN",
        ),
        lambda incident, round_record: Evidence(
            incident_id=incident.id,
            round_id=round_record.id,
            evidence_type="metric",
            source="prometheus",
            content="cross-incident evidence",
        ),
        lambda incident, round_record: ToolCall(
            incident_id=incident.id,
            round_id=round_record.id,
            tool_name="query_metrics",
            status="SUCCESS",
        ),
        lambda incident, round_record: Report(
            incident_id=incident.id,
            round_id=round_record.id,
            version=1,
            content={"cross_incident": True},
        ),
    ],
    ids=["observation", "hypothesis", "evidence", "tool-call", "report"],
)
def test_round_owned_record_cannot_bind_another_incidents_round(
    database_session: Session, record_factory
) -> None:
    first_incident, _, other_incident_round = _two_incidents(database_session)
    database_session.add(record_factory(first_incident, other_incident_round))

    with pytest.raises(IntegrityError):
        database_session.commit()
    database_session.rollback()


def test_v1_compatibility_records_bind_to_the_incident_thread_round(
    database_session: Session,
) -> None:
    target, service = _target_with_service()
    incident = _incident(target, service)
    database_session.add(incident)
    database_session.commit()
    first_round = incident.rounds[0]
    second_round = InvestigationRound(
        incident_id=incident.id,
        round_number=2,
        status="INVESTIGATING",
        thread_id=str(uuid4()),
    )
    database_session.add(second_round)
    database_session.commit()

    hypothesis = Hypothesis(
        incident_id=incident.id,
        summary="V1 compatibility hypothesis",
        status="OPEN",
    )
    evidence = Evidence(
        incident_id=incident.id,
        evidence_type="metric",
        source="prometheus",
        content="V1 compatibility evidence",
    )
    tool_call = ToolCall(
        incident_id=incident.id,
        tool_name="query_metrics",
        status="SUCCESS",
    )
    report = Report(incident_id=incident.id, content={"compatibility": True})
    database_session.add_all([hypothesis, evidence, tool_call, report])
    database_session.commit()

    assert first_round.thread_id == incident.thread_id
    assert second_round.thread_id != incident.thread_id
    assert {hypothesis.round_id, evidence.round_id, tool_call.round_id, report.round_id} == {
        first_round.id
    }


def test_round_owned_records_observations_and_versioned_reports(database_session: Session) -> None:
    target, service = _target_with_service()
    incident = _incident(target, service)
    database_session.add(incident)
    database_session.commit()
    first_round = incident.rounds[0]
    second_round = InvestigationRound(
        incident_id=incident.id,
        round_number=2,
        status="INVESTIGATING",
        thread_id=str(uuid4()),
    )
    observation = Observation(
        incident_id=incident.id,
        round=second_round,
        content="第二次观察到支付超时。",
        observed_at=datetime(2026, 9, 19, 10, tzinfo=UTC),
        context_data={"source": "on-call"},
    )
    incident_only_observation = Observation(
        incident_id=incident.id,
        content="初始报警仍在持续。",
        observed_at=datetime(2026, 9, 19, 9, tzinfo=UTC),
    )
    hypothesis = Hypothesis(
        incident_id=incident.id,
        round=second_round,
        summary="下游支付服务响应变慢。",
        status="OPEN",
    )
    evidence = Evidence(
        incident_id=incident.id,
        round=second_round,
        evidence_type="metric",
        source="prometheus",
        content="payment latency increased",
    )
    tool_call = ToolCall(
        incident_id=incident.id,
        round=second_round,
        tool_name="query_metrics",
        status="SUCCESS",
    )
    first_report = Report(
        incident_id=incident.id,
        round=first_round,
        version=1,
        content={"round": 1},
    )
    second_report = Report(
        incident_id=incident.id,
        round=second_round,
        version=2,
        content={"round": 2},
    )
    database_session.add_all(
        [
            second_round,
            observation,
            incident_only_observation,
            hypothesis,
            evidence,
            tool_call,
            first_report,
            second_report,
        ]
    )
    database_session.commit()

    assert observation.incident is incident
    assert observation.round is second_round
    assert incident_only_observation.round is None
    assert hypothesis.round is second_round
    assert evidence.round is second_round
    assert tool_call.round is second_round
    assert {report.version for report in incident.reports} == {1, 2}
    assert first_round.thread_id != second_round.thread_id

    database_session.add(
        Report(incident_id=incident.id, round=second_round, version=3, content={"duplicate": True})
    )
    with pytest.raises(IntegrityError):
        database_session.commit()
    database_session.rollback()

    database_session.add(
        InvestigationRound(
            incident_id=incident.id,
            round_number=3,
            status="OPEN",
            thread_id=first_round.thread_id,
        )
    )
    with pytest.raises(IntegrityError):
        database_session.commit()
    database_session.rollback()

    database_session.add(
        InvestigationRound(
            incident_id=incident.id,
            round_number=2,
            status="OPEN",
            thread_id=str(uuid4()),
        )
    )
    with pytest.raises(IntegrityError):
        database_session.commit()
    database_session.rollback()
