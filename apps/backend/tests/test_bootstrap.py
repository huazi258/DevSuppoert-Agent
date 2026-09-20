"""Regression coverage for fresh-database V2 target bootstrap."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.bootstrap import BootstrapConflictError, bootstrap_investigation_targets
from devsupport_backend.models import InvestigationTarget, Service
from devsupport_backend.target_config import (
    CapabilityConfig,
    InvestigationTargetConfig,
    TargetServiceConfig,
)


def _target_config(*, target_id=None, slug: str | None = None) -> InvestigationTargetConfig:
    return InvestigationTargetConfig(
        target_id=target_id or uuid4(),
        slug=slug or f"bootstrap-{uuid4()}",
        display_name="本地浏览器验收环境",
        description="用于验证 V2 的最小本地调查环境。",
        environment="trusted-local",
        services=(
            TargetServiceConfig(
                name="checkout",
                display_name="结算服务",
                description="处理结算请求。",
            ),
        ),
        logs=CapabilityConfig(),
        metrics=CapabilityConfig(),
    )


def test_bootstrap_creates_and_reconciles_configured_target_and_service(
    database_session: Session,
) -> None:
    config = _target_config()

    first = bootstrap_investigation_targets(database_session, [config])
    second = bootstrap_investigation_targets(database_session, [config])

    target = database_session.get(InvestigationTarget, config.target_id)
    services = list(
        database_session.scalars(select(Service).where(Service.target_id == config.target_id))
    )
    assert first.created_targets == 1
    assert first.created_services == 1
    assert second.created_targets == second.created_services == 0
    assert second.updated_targets == second.updated_services == 1
    assert target is not None
    assert target.slug == config.slug
    assert target.name == "本地浏览器验收环境"
    assert target.environment == "trusted-local"
    assert [(service.name, service.display_name, service.enabled) for service in services] == [
        ("checkout", "结算服务", True)
    ]


def test_bootstrap_uses_safe_display_name_fallback(database_session: Session) -> None:
    config = _target_config(slug="orders_local")
    config = config.model_copy(update={"display_name": None, "description": None})

    bootstrap_investigation_targets(database_session, [config])

    target = database_session.get(InvestigationTarget, config.target_id)
    assert target is not None
    assert target.name == "Orders Local"
    assert target.description is None


def test_bootstrap_rejects_slug_owned_by_another_target(database_session: Session) -> None:
    existing = InvestigationTarget(
        name="Existing target",
        slug="shared-slug",
        environment="local",
        enabled=True,
    )
    database_session.add(existing)
    database_session.commit()
    config = _target_config(slug="shared-slug")

    with pytest.raises(BootstrapConflictError, match="belongs to a different"):
        bootstrap_investigation_targets(database_session, [config])

    assert database_session.get(InvestigationTarget, config.target_id) is None
