"""V2 product lifecycle statuses and the explicitly isolated V1 projection mapping."""

from enum import Enum


class InvestigationStatus(str, Enum):
    """The only statuses exposed by the V2 Incident and InvestigationRound lifecycle."""

    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    CONCLUDED = "CONCLUDED"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAILED = "FAILED"


TERMINAL_INVESTIGATION_STATUSES = frozenset(
    {
        InvestigationStatus.CONCLUDED,
        InvestigationStatus.INCONCLUSIVE,
        InvestigationStatus.FAILED,
    }
)


def legacy_status_projection(legacy_status: str | None) -> InvestigationStatus:
    """Project a retained V1 remediation status without treating it as a V2 status.

    The legacy value remains stored in ``Incident.status`` for V1-only workflow consumers.
    In particular, ``RESOLVED`` becomes a V2 investigation conclusion, never a claim that a
    business system is currently restored.
    """
    mapping = {
        "OPEN": InvestigationStatus.OPEN,
        "INVESTIGATING": InvestigationStatus.INVESTIGATING,
        "WAITING_APPROVAL": InvestigationStatus.INVESTIGATING,
        "REMEDIATING": InvestigationStatus.INVESTIGATING,
        "VERIFYING": InvestigationStatus.INVESTIGATING,
        "RESOLVED": InvestigationStatus.CONCLUDED,
        "NEEDS_MANUAL_ACTION": InvestigationStatus.INCONCLUSIVE,
    }
    return mapping.get(legacy_status, InvestigationStatus.FAILED)
