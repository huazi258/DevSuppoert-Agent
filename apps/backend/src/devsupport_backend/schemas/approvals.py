"""Strict HTTP contracts for human decisions on a server-selected Action."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from devsupport_backend.agent.state import ApprovalStatus


class ApprovalDecision(StrEnum):
    """The only human decisions accepted by the Approval API."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"


class ApprovalCreate(BaseModel):
    """A decision only; the server identifies the pending Action from the checkpoint."""

    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision


class ApprovalResponse(BaseModel):
    """Public record of a final decision bound to its exact Action."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    incident_id: UUID
    action_id: UUID
    status: ApprovalStatus
