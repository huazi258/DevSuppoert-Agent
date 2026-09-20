"""Idempotent deployment bootstrap for V2 investigation selection records."""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.config import settings
from devsupport_backend.database import SessionLocal
from devsupport_backend.models import InvestigationTarget, Service
from devsupport_backend.target_config import InvestigationTargetConfig


class BootstrapConflictError(RuntimeError):
    """Deployment configuration conflicts with a separately persisted target identity."""


@dataclass(frozen=True)
class BootstrapResult:
    """Safe, non-secret summary of one target/service reconciliation run."""

    created_targets: int = 0
    updated_targets: int = 0
    created_services: int = 0
    updated_services: int = 0


def bootstrap_investigation_targets(
    session: Session, target_configs: list[InvestigationTargetConfig]
) -> BootstrapResult:
    """Reconcile deployment-owned target and service selection records atomically.

    Configuration owns the stable target ID, slug, environment, display metadata and
    service allowlist.  Existing records are updated in place so an Incident's
    historical target/service foreign keys remain stable.  Targets or services removed
    from configuration are deliberately retained: they may be referenced by immutable
    investigation history and are not selectable without a matching configuration.
    """

    result = BootstrapResult()
    try:
        for config in target_configs:
            result = _bootstrap_target(session, config, result)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return result


def _bootstrap_target(
    session: Session, config: InvestigationTargetConfig, result: BootstrapResult
) -> BootstrapResult:
    slug_owner = session.scalar(
        select(InvestigationTarget).where(InvestigationTarget.slug == config.slug)
    )
    target = session.get(InvestigationTarget, config.target_id)
    if slug_owner is not None and slug_owner.id != config.target_id:
        raise BootstrapConflictError(
            f"configured slug {config.slug!r} belongs to a different InvestigationTarget"
        )
    if target is None:
        target = InvestigationTarget(id=config.target_id)
        session.add(target)
        created_targets = result.created_targets + 1
        updated_targets = result.updated_targets
    else:
        created_targets = result.created_targets
        updated_targets = result.updated_targets + 1

    target.name = config.display_name or _display_name(config.slug)
    target.slug = config.slug
    target.description = config.description
    target.environment = config.environment
    target.enabled = True
    session.flush()

    created_services = result.created_services
    updated_services = result.updated_services
    for configured_service in config.services:
        service = session.scalar(
            select(Service).where(
                Service.target_id == target.id,
                Service.name == configured_service.name,
            )
        )
        if service is None:
            service = Service(target_id=target.id, name=configured_service.name)
            session.add(service)
            created_services += 1
        else:
            updated_services += 1
        service.display_name = configured_service.display_name or configured_service.name
        service.description = configured_service.description
        service.enabled = True

    return BootstrapResult(
        created_targets=created_targets,
        updated_targets=updated_targets,
        created_services=created_services,
        updated_services=updated_services,
    )


def _display_name(slug: str) -> str:
    """Provide a readable fallback while allowing deployment config to supply Chinese copy."""

    return slug.replace("-", " ").replace("_", " ").title()


def main() -> None:
    """Bootstrap configured target/service records after Alembic migrations."""

    parser = argparse.ArgumentParser(
        description="Synchronize configured V2 InvestigationTarget and Service records"
    )
    parser.parse_args()
    with SessionLocal() as session:
        result = bootstrap_investigation_targets(session, settings.investigation_target_configs)
    print(
        "V2 bootstrap complete: "
        f"created_targets={result.created_targets}, updated_targets={result.updated_targets}, "
        f"created_services={result.created_services}, updated_services={result.updated_services}"
    )


if __name__ == "__main__":
    main()
