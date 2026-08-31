"""Pure V1 user-facing investigation narrative derived from persisted checkpoints."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from devsupport_backend.agent.runtime import WorkflowCheckpointRecord, WorkflowFailure
from devsupport_backend.agent.state import AgentStage, AgentState, HypothesisContext, TerminalReason
from devsupport_backend.models import Incident
from devsupport_backend.schemas.workflows import (
    InvestigationTimelineEventResponse,
    InvestigationTimelineEventType,
)

_TERMINAL_STAGES = frozenset({AgentStage.RESOLVED, AgentStage.NEEDS_MANUAL_ACTION})
_TOOL_NARRATIVES = {
    "search_knowledge": ("knowledge_searched", "Knowledge searched", "knowledge"),
    "query_logs": ("evidence_collected", "Logs collected", "log"),
    "query_metrics": ("evidence_collected", "Metrics collected", "metric"),
    "query_traces": ("evidence_collected", "Traces collected", "trace"),
    "get_deployment_history": (
        "evidence_collected",
        "Deployment history checked",
        "deployment",
    ),
}


def project_investigation_timeline(
    incident: Incident,
    records: Iterable[WorkflowCheckpointRecord],
    current_failure: WorkflowFailure | None,
    *,
    truncated: bool,
) -> list[InvestigationTimelineEventResponse]:
    """Project only user-visible state changes; this is a narrative, not an audit log."""
    history = list(records)
    if not history:
        return []

    emitted: list[tuple[datetime, int, int, InvestigationTimelineEventResponse]] = []
    previous: AgentState | None = None
    start_index = 0
    if truncated:
        # Earlier state is unavailable, so do not invent first-seen facts from this bounded frame.
        previous = history[0].state
        start_index = 1

    for index in range(start_index, len(history)):
        record = history[index]
        current = record.state
        events = _events_for_state_change(incident, previous, current, record.created_at, index)
        for priority, event in events:
            emitted.append((record.created_at, priority, index, event))
        previous = current

    if current_failure is not None and not any(
        event.event_type == "investigation_interrupted" for _, _, _, event in emitted
    ):
        latest = history[-1]
        emitted.append(
            (
                latest.created_at,
                45,
                len(history),
                _interruption_event(
                    event_id=(
                        f"interruption:current:{current_failure.failed_node}:"
                        f"{current_failure.category.value}"
                    ),
                    occurred_at=latest.created_at,
                    category=current_failure.category.value,
                    message=current_failure.safe_error,
                    retryable=current_failure.retryable,
                ),
            )
        )

    emitted.sort(key=lambda item: (item[0], item[1], item[2], item[3].event_id))
    return [
        event.model_copy(update={"sequence": sequence})
        for sequence, (_, _, _, event) in enumerate(emitted, 1)
    ]


def _events_for_state_change(
    incident: Incident,
    previous: AgentState | None,
    current: AgentState,
    occurred_at: datetime,
    history_index: int,
) -> list[tuple[int, InvestigationTimelineEventResponse]]:
    events: list[tuple[int, InvestigationTimelineEventResponse]] = []
    if previous is None:
        events.append(
            (
                0,
                _event(
                    event_id=f"investigation-started:{incident.id}",
                    event_type="investigation_started",
                    occurred_at=occurred_at,
                    title="Investigation started",
                    summary="Investigation state was first persisted.",
                ),
            )
        )

    events.extend(_tool_events(previous, current, occurred_at))
    events.extend(_hypothesis_events(previous, current, occurred_at, history_index))

    if previous is None or (
        previous["final_conclusion"] is None and current["final_conclusion"] is not None
    ):
        conclusion = current["final_conclusion"]
        if conclusion is not None:
            events.append(
                (
                    50,
                    _event(
                        event_id="conclusion-reached",
                        event_type="conclusion_reached",
                        occurred_at=occurred_at,
                        title="Conclusion reached",
                        summary=conclusion.summary,
                    ),
                )
            )

    if previous is None or (
        previous["proposed_action"] is None and current["proposed_action"] is not None
    ):
        proposed_action = current["proposed_action"]
        if proposed_action is not None:
            events.append(
                (
                    60,
                    _event(
                        event_id="action-proposed",
                        event_type="action_proposed",
                        occurred_at=occurred_at,
                        title="Action proposed",
                        summary=proposed_action.summary,
                    ),
                )
            )

    if previous is None or (
        previous["policy_outcome"] is None and current["policy_outcome"] is not None
    ):
        policy = current["policy_outcome"]
        if policy is not None:
            events.append(
                (
                    70,
                    _event(
                        event_id="policy-decision",
                        event_type="policy_decision",
                        occurred_at=occurred_at,
                        title="Policy decision",
                        summary=policy.reason,
                        status=policy.decision.value,
                    ),
                )
            )

    previous_stage = previous["current_stage"] if previous is not None else None
    if (
        previous_stage is not AgentStage.WAITING_APPROVAL
        and current["current_stage"] is AgentStage.WAITING_APPROVAL
    ):
        events.append(
            (
                80,
                _event(
                    event_id="approval-wait",
                    event_type="approval_wait",
                    occurred_at=occurred_at,
                    title="Approval required",
                    summary="The proposed action is waiting for a human decision.",
                ),
            )
        )

    if previous is None or (
        previous["approval_outcome"] is None and current["approval_outcome"] is not None
    ):
        approval = current["approval_outcome"]
        if approval is not None:
            events.append(
                (
                    90,
                    _event(
                        event_id=f"approval-decision:{approval.approval_id}",
                        event_type="approval_decision",
                        occurred_at=occurred_at,
                        title="Approval decision recorded",
                        summary="A human approval decision was recorded.",
                        status=approval.status.value,
                    ),
                )
            )

    if previous is None or (
        previous["execution_outcome"] is None and current["execution_outcome"] is not None
    ):
        execution = current["execution_outcome"]
        if execution is not None:
            succeeded = execution.executed
            events.append(
                (
                    100,
                    _event(
                        event_id=f"action-execution:{execution.action_id or 'none'}",
                        event_type="action_execution",
                        occurred_at=occurred_at,
                        title="Action executed" if succeeded else "Action execution failed",
                        summary=(
                            "The approved action was executed."
                            if succeeded
                            else "The approved action did not complete."
                        ),
                        status=execution.status.value,
                    ),
                )
            )

    if previous is None or (
        previous["verification_outcome"] is None and current["verification_outcome"] is not None
    ):
        verification = current["verification_outcome"]
        if verification is not None:
            events.append(
                (
                    110,
                    _event(
                        event_id=f"recovery-verification:{verification.verification_id or 'none'}",
                        event_type="recovery_verification",
                        occurred_at=occurred_at,
                        title="Recovery verification",
                        summary=verification.summary,
                        status=verification.status.value,
                    ),
                )
            )

    events.extend(_historical_failure_events(previous, current, occurred_at, history_index))
    terminal_before = previous is not None and _is_terminal(previous)
    if not terminal_before and _is_terminal(current):
        final_status = (
            "RESOLVED" if current["current_stage"] is AgentStage.RESOLVED else "NEEDS_MANUAL_ACTION"
        )
        events.append(
            (
                120,
                _event(
                    event_id=f"investigation-completed:{final_status}",
                    event_type="investigation_completed",
                    occurred_at=occurred_at,
                    title="Investigation completed",
                    summary=_terminal_summary(current.get("terminal_reason")),
                    status=final_status,
                ),
            )
        )
    return events


def _tool_events(
    previous: AgentState | None,
    current: AgentState,
    occurred_at: datetime,
) -> list[tuple[int, InvestigationTimelineEventResponse]]:
    previous_count = len(previous["tool_history"]) if previous is not None else 0
    events: list[tuple[int, InvestigationTimelineEventResponse]] = []
    for index, tool in enumerate(current["tool_history"][previous_count:], previous_count):
        event_type, title, noun = _TOOL_NARRATIVES.get(
            tool.tool_name.value,
            ("evidence_collected", "Runtime evidence collected", "runtime"),
        )
        if tool.status.value == "success":
            summary = f"Collected {len(tool.evidence_ids)} {noun} evidence record(s)."
            event_title = title
        else:
            event_title = f"{title} failed"
            summary = (
                tool.error.message
                if tool.error is not None
                else "The requested evidence was unavailable."
            )
        events.append(
            (
                10,
                _event(
                    event_id=f"tool:{index}",
                    event_type=event_type,
                    occurred_at=occurred_at,
                    title=event_title,
                    summary=summary,
                    status=tool.status.value,
                ),
            )
        )
    return events


def _hypothesis_events(
    previous: AgentState | None,
    current: AgentState,
    occurred_at: datetime,
    history_index: int,
) -> list[tuple[int, InvestigationTimelineEventResponse]]:
    previous_by_id = {item.id: item for item in previous["hypotheses"]} if previous else {}
    events: list[tuple[int, InvestigationTimelineEventResponse]] = []
    for hypothesis in current["hypotheses"]:
        earlier = previous_by_id.get(hypothesis.id)
        if earlier is None:
            events.append(
                (
                    20,
                    _event(
                        event_id=f"hypothesis-created:{hypothesis.id}",
                        event_type="hypothesis_created",
                        occurred_at=occurred_at,
                        title="Hypothesis created",
                        summary=hypothesis.summary,
                        status=hypothesis.status.value,
                    ),
                )
            )
        elif _hypothesis_changed(earlier, hypothesis):
            events.append(
                (
                    30,
                    _event(
                        event_id=f"hypothesis-updated:{hypothesis.id}:{history_index}:{occurred_at.isoformat()}",
                        event_type="hypothesis_updated",
                        occurred_at=occurred_at,
                        title=_hypothesis_update_title(earlier, hypothesis),
                        summary=hypothesis.summary,
                        status=hypothesis.status.value,
                    ),
                )
            )
    return events


def _hypothesis_changed(previous: HypothesisContext, current: HypothesisContext) -> bool:
    return (
        previous.status != current.status
        or previous.confidence != current.confidence
        or previous.supporting_evidence_ids != current.supporting_evidence_ids
        or previous.contradicting_evidence_ids != current.contradicting_evidence_ids
        or previous.next_check != current.next_check
    )


def _hypothesis_update_title(previous: HypothesisContext, current: HypothesisContext) -> str:
    if previous.status != current.status:
        return {
            "SUPPORTED": "Hypothesis supported",
            "REJECTED": "Hypothesis rejected",
            "CONFIRMED": "Hypothesis confirmed",
        }.get(current.status.value, "Hypothesis updated")
    return "Hypothesis updated"


def _historical_failure_events(
    previous: AgentState | None,
    current: AgentState,
    occurred_at: datetime,
    history_index: int,
) -> list[tuple[int, InvestigationTimelineEventResponse]]:
    before = _safe_failure_facts(previous) if previous is not None else None
    after = _safe_failure_facts(current)
    if after is None or after == before:
        return []
    category, message, retryable = after
    return [
        (
            45,
            _interruption_event(
                event_id=f"interruption:{history_index}:{category}",
                occurred_at=occurred_at,
                category=category,
                message=message,
                retryable=retryable,
            ),
        )
    ]


def _safe_failure_facts(state: AgentState) -> tuple[str, str, bool] | None:
    category = state.get("workflow_failure_category")
    message = state.get("workflow_failure_safe_message")
    retryable = state.get("workflow_failure_retryable")
    category_value = getattr(category, "value", category)
    if not isinstance(category_value, str) or not isinstance(message, str) or not message.strip():
        return None
    return category_value, message, retryable if isinstance(retryable, bool) else False


def _interruption_event(
    *,
    event_id: str,
    occurred_at: datetime,
    category: str,
    message: str,
    retryable: bool,
) -> InvestigationTimelineEventResponse:
    return _event(
        event_id=event_id,
        event_type="investigation_interrupted",
        occurred_at=occurred_at,
        title="Investigation interrupted",
        summary=message,
        status=category,
    )


def _is_terminal(state: AgentState) -> bool:
    return state["current_stage"] in _TERMINAL_STAGES


def _terminal_summary(reason: TerminalReason | None) -> str:
    return {
        TerminalReason.ACTIVE_EXECUTION_BUDGET_EXHAUSTED: (
            "Investigation ended at its execution limit."
        ),
        TerminalReason.LLM_CALL_BUDGET_EXHAUSTED: "Investigation ended at its analysis limit.",
        TerminalReason.INVESTIGATION_ROUND_LIMIT_REACHED: (
            "Investigation ended after its planned rounds."
        ),
        TerminalReason.TOOL_CALL_LIMIT_REACHED: (
            "Investigation ended after its evidence collection limit."
        ),
        TerminalReason.POLICY_DENIED: "Investigation completed without an approved action.",
        TerminalReason.APPROVAL_REJECTED: (
            "Investigation completed after the proposed action was rejected."
        ),
    }.get(reason, "Investigation reached a final state.")


def _event(
    *,
    event_id: str,
    event_type: InvestigationTimelineEventType,
    occurred_at: datetime,
    title: str,
    summary: str,
    status: str | None = None,
) -> InvestigationTimelineEventResponse:
    return InvestigationTimelineEventResponse(
        event_id=event_id,
        sequence=0,
        event_type=event_type,
        occurred_at=occurred_at,
        title=title,
        summary=summary,
        status=status,
    )
