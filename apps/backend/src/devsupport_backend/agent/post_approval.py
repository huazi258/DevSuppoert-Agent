"""Shared post-approval continuation topology for formal and resumed workflows."""

from typing import Protocol

from langgraph.graph import END, StateGraph

from devsupport_backend.agent.state import AgentStage, AgentState


class ControlledActionExecution(Protocol):
    """The one injected boundary permitted to execute an approved Action."""

    def execute(self, state: AgentState):
        """Return the checkpoint-safe outcome for this Action."""


class RecoveryVerification(Protocol):
    def verify(self, state: AgentState):
        """Persist deterministic recovery evidence for one executed Action."""


class FinalReport(Protocol):
    def generate(self, state: AgentState):
        """Persist a deterministic terminal audit projection."""


def add_post_approval_continuation(
    graph: StateGraph,
    *,
    action_execution: ControlledActionExecution,
    recovery_verification: RecoveryVerification | None = None,
    final_report: FinalReport | None = None,
) -> None:
    """Use identical node names and edges after approval in both graph entry paths."""
    from devsupport_backend.action_execution import controlled_action_execution_node

    graph.add_node(
        "controlled_action_execution",
        lambda state: controlled_action_execution_node(state, action_execution),
    )
    graph.add_conditional_edges(
        "approval_decision",
        _route_after_approval_decision
        if final_report is not None
        else _route_approval_without_report,
        {
            "controlled_action_execution": "controlled_action_execution",
            "final_report": "final_report",
            "end": END,
        },
    )
    if final_report is not None:
        from devsupport_backend.final_report import final_report_node

        graph.add_node("final_report", lambda state: final_report_node(state, final_report))
        graph.add_edge("final_report", END)
    if recovery_verification is None:
        graph.add_conditional_edges(
            "controlled_action_execution",
            _route_after_execution,
            {"final_report": "final_report", "end": END},
        ) if final_report is not None else graph.add_edge("controlled_action_execution", END)
        return
    from devsupport_backend.recovery_verification import recovery_verification_node

    graph.add_node(
        "recovery_verification",
        lambda state: recovery_verification_node(state, recovery_verification),
    )
    graph.add_conditional_edges(
        "controlled_action_execution",
        lambda state: (
            "recovery_verification"
            if state["current_stage"] is AgentStage.RECOVERY_VERIFICATION
            else "end"
        ),
        {
            "recovery_verification": "recovery_verification",
            "final_report": "final_report",
            "end": END,
        },
    )
    graph.add_edge("recovery_verification", "final_report" if final_report is not None else END)


def _route_after_approval_decision(state: AgentState) -> str:
    return (
        "controlled_action_execution"
        if state["current_stage"] is AgentStage.ACTION_EXECUTION
        else "final_report"
        if state["current_stage"] is AgentStage.NEEDS_MANUAL_ACTION
        else "end"
    )


def _route_after_execution(state: AgentState) -> str:
    return "final_report" if state["current_stage"] is AgentStage.NEEDS_MANUAL_ACTION else "end"


def _route_approval_without_report(state: AgentState) -> str:
    return (
        "controlled_action_execution"
        if state["current_stage"] is AgentStage.ACTION_EXECUTION
        else "end"
    )
