"""LangGraph wiring and loop controls for the bounded investigation workflow."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.orm import Session

from devsupport_backend.agent.llm import LLMClient
from devsupport_backend.agent.nodes.hypothesis_generation import hypothesis_generation_node
from devsupport_backend.agent.nodes.hypothesis_update import hypothesis_update_node
from devsupport_backend.agent.nodes.intake import intake_node
from devsupport_backend.agent.nodes.planner import investigation_planner_node
from devsupport_backend.agent.nodes.retrieval import retrieval_node
from devsupport_backend.agent.nodes.tool_execution import (
    ToolExecutionDependencies,
    tool_execution_node,
)
from devsupport_backend.agent.policy import PolicyGate, policy_gate_node
from devsupport_backend.agent.post_approval import (
    ControlledActionExecution,
    FinalReport,
    RecoveryVerification,
    add_final_report_node,
    add_post_approval_continuation,
)
from devsupport_backend.agent.resolution_proposal import resolution_proposal_node
from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    EvaluationDecision,
    PolicyDecision,
)
from devsupport_backend.approvals import (
    ApprovalDecisionService,
    ApprovalWait,
    approval_decision_node,
    approval_interrupt_node,
    approval_wait_node,
)
from devsupport_backend.rag.retrieval import RAGService

DEFAULT_MAX_INVESTIGATION_ROUNDS = 5
DEFAULT_MAX_TOOL_CALLS = 6
"""Conservative V0 workflow limits, kept central rather than in graph edges."""


class EvidenceEvaluator(Protocol):
    """Task 3.8 contract; Task 3.9 will provide its real evidence evaluation logic."""

    def evaluate(self, state: AgentState) -> EvaluationDecision:
        """Return the bounded decision for the current post-update investigation state."""


class ManualTerminalizer(Protocol):
    """Persist the deterministic terminal lifecycle before a terminal report is projected."""

    def mark_needs_manual_action(self, state: AgentState) -> AgentState:
        """Return state at NEEDS_MANUAL_ACTION after updating the authoritative Incident."""


class InvestigationWorkflowError(RuntimeError):
    """Raised when a Task 3.8 workflow collaborator violates its explicit contract."""


@dataclass(frozen=True)
class InvestigationLoopLimits:
    """Hard safety limits for planner and Tool execution within one investigation."""

    max_rounds: int = DEFAULT_MAX_INVESTIGATION_ROUNDS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        if self.max_tool_calls < 1:
            raise ValueError("max_tool_calls must be at least 1")


@dataclass(frozen=True)
class InvestigationWorkflowDependencies:
    """Explicit existing-node dependencies; no graph node accesses infrastructure directly."""

    rag_service: RAGService
    llm_client: LLMClient
    tool_execution: ToolExecutionDependencies
    evaluator: EvidenceEvaluator
    policy_gate: PolicyGate
    approval_wait: ApprovalWait
    approval_decision: ApprovalDecisionService
    action_execution: ControlledActionExecution | None = None
    recovery_verification: RecoveryVerification | None = None
    final_report: FinalReport | None = None
    manual_terminalizer: ManualTerminalizer | None = None


def build_investigation_graph(
    dependencies: InvestigationWorkflowDependencies,
    *,
    limits: InvestigationLoopLimits | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """Compile the bounded Day 3 graph with an optional externally owned checkpointer."""
    loop_limits = limits or InvestigationLoopLimits()
    graph = StateGraph(AgentState)

    graph.add_node("intake", intake_node)
    graph.add_node("retrieval", lambda state: retrieval_node(state, dependencies.rag_service))
    graph.add_node(
        "hypothesis_generation",
        lambda state: hypothesis_generation_node(state, dependencies.llm_client),
    )
    graph.add_node(
        "planning_guard",
        lambda state: _planning_guard_node(state, loop_limits),
    )
    graph.add_node(
        "investigation_planning",
        lambda state: investigation_planner_node(state, dependencies.llm_client),
    )
    graph.add_node(
        "tool_execution",
        lambda state: tool_execution_node(state, dependencies.tool_execution),
    )
    graph.add_node(
        "hypothesis_update",
        lambda state: _hypothesis_update_round_node(state, dependencies.llm_client),
    )
    graph.add_node(
        "evidence_evaluation",
        lambda state: evidence_evaluation_node(state, dependencies.evaluator, loop_limits),
    )
    graph.add_node(
        "resolution_proposal",
        lambda state: resolution_proposal_node(state, dependencies.llm_client),
    )
    graph.add_node(
        "policy_gate",
        lambda state: policy_gate_node(state, dependencies.policy_gate),
    )
    graph.add_node(
        "approval_wait",
        lambda state: approval_wait_node(state, dependencies.approval_wait),
    )
    graph.add_node(
        "approval_interrupt",
        lambda state: approval_interrupt_node(state, dependencies.approval_wait),
    )
    graph.add_node(
        "approval_decision",
        lambda state: approval_decision_node(state, dependencies.approval_decision),
    )
    has_terminal_report = (
        dependencies.final_report is not None and dependencies.manual_terminalizer is not None
    )
    if has_terminal_report:
        graph.add_node(
            "manual_terminalization",
            lambda state: dependencies.manual_terminalizer.mark_needs_manual_action(state),
        )
        if dependencies.action_execution is None:
            add_final_report_node(graph, dependencies.final_report)
        graph.add_edge("manual_terminalization", "final_report")
    else:
        # Keep route targets valid in pure Day 3 unit graphs without a report boundary.
        graph.add_node("manual_terminalization", lambda state: state)
        graph.add_edge("manual_terminalization", END)
    if dependencies.action_execution is not None:
        add_post_approval_continuation(
            graph,
            action_execution=dependencies.action_execution,
            recovery_verification=dependencies.recovery_verification,
            final_report=dependencies.final_report,
        )

    graph.add_edge(START, "intake")
    graph.add_conditional_edges(
        "intake",
        _route_after_intake,
        {"retrieval": "retrieval", "end": END},
    )
    graph.add_conditional_edges(
        "retrieval",
        _route_after_retrieval,
        {"hypothesis_generation": "hypothesis_generation", "end": END},
    )
    graph.add_conditional_edges(
        "hypothesis_generation",
        _route_after_hypothesis_generation,
        {"planning_guard": "planning_guard", "end": END},
    )
    graph.add_conditional_edges(
        "planning_guard",
        lambda state: _route_after_planning_guard(state, has_terminal_report),
        {
            "investigation_planning": "investigation_planning",
            "manual_terminalization": "manual_terminalization",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "investigation_planning",
        _route_after_planning,
        {"tool_execution": "tool_execution", "end": END},
    )
    graph.add_conditional_edges(
        "tool_execution",
        _route_after_tool_execution,
        {"hypothesis_update": "hypothesis_update", "planning_guard": "planning_guard", "end": END},
    )
    graph.add_conditional_edges(
        "hypothesis_update",
        _route_after_hypothesis_update,
        {"evidence_evaluation": "evidence_evaluation", "end": END},
    )
    graph.add_conditional_edges(
        "evidence_evaluation",
        lambda state: _route_after_evidence_evaluation(state, has_terminal_report),
        {
            "planning_guard": "planning_guard",
            "resolution_proposal": "resolution_proposal",
            "manual_terminalization": "manual_terminalization",
            "end": END,
        },
    )
    graph.add_edge("resolution_proposal", "policy_gate")
    graph.add_conditional_edges(
        "policy_gate",
        lambda state: _route_after_policy_gate(state, has_terminal_report),
        {
            "approval_wait": "approval_wait",
            "manual_terminalization": "manual_terminalization",
            "end": END,
        },
    )
    graph.add_edge("approval_wait", "approval_interrupt")
    graph.add_edge("approval_interrupt", "approval_decision")
    if dependencies.action_execution is None:
        graph.add_edge("approval_decision", END)
    return graph.compile(checkpointer=checkpointer)


def build_production_investigation_graph(
    dependencies: InvestigationWorkflowDependencies,
    *,
    session: Session,
    limits: InvestigationLoopLimits | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """Compose the formal start graph with its PostgreSQL terminal report boundaries."""
    from devsupport_backend.agent.terminalization import PostgresManualTerminalizer
    from devsupport_backend.final_report import FinalReportService

    return build_investigation_graph(
        replace(
            dependencies,
            final_report=FinalReportService(session),
            manual_terminalizer=PostgresManualTerminalizer(session),
        ),
        limits=limits,
        checkpointer=checkpointer,
    )


def evidence_evaluation_node(
    state: AgentState,
    evaluator: EvidenceEvaluator,
    limits: InvestigationLoopLimits,
) -> AgentState:
    """Evaluate completed evidence before the planning guard limits any new round."""
    if state["current_stage"] != AgentStage.EVIDENCE_EVALUATION:
        return state

    decision = evaluator.evaluate(state)
    if not isinstance(decision, EvaluationDecision):
        raise InvestigationWorkflowError("evaluator returned an invalid decision")
    return {
        **state,
        "evaluation_decision": decision,
        "current_stage": (
            AgentStage.INVESTIGATION_PLANNING
            if decision == EvaluationDecision.CONTINUE
            else state["current_stage"]
        ),
    }


def _hypothesis_update_round_node(state: AgentState, llm_client: LLMClient) -> AgentState:
    """Let the existing node own updates, then count exactly one completed success path."""
    updated = hypothesis_update_node(state, llm_client)
    if (
        state["current_stage"] == AgentStage.HYPOTHESIS_UPDATE
        and updated["current_stage"] == AgentStage.EVIDENCE_EVALUATION
    ):
        return {
            **updated,
            "investigation_round": state["investigation_round"] + 1,
        }
    return updated


def _planning_guard_node(state: AgentState, limits: InvestigationLoopLimits) -> AgentState:
    """Stop before calling Planner when a business safety limit has already been reached."""
    if state["current_stage"] == AgentStage.INVESTIGATION_PLANNING and _limits_reached(
        state, limits
    ):
        return {**state, "evaluation_decision": EvaluationDecision.NEEDS_MANUAL_ACTION}
    return state


def _limits_reached(state: AgentState, limits: InvestigationLoopLimits) -> bool:
    """Keep workflow limits independent from LangGraph's recursion protection."""
    return (
        state["investigation_round"] >= limits.max_rounds
        or state["tool_call_count"] >= limits.max_tool_calls
    )


def _route_after_intake(state: AgentState) -> str:
    return "retrieval" if state["current_stage"] == AgentStage.RETRIEVAL else "end"


def _route_after_retrieval(state: AgentState) -> str:
    return (
        "hypothesis_generation"
        if state["current_stage"] == AgentStage.HYPOTHESIS_GENERATION
        else "end"
    )


def _route_after_hypothesis_generation(state: AgentState) -> str:
    return (
        "planning_guard"
        if state["current_stage"] == AgentStage.INVESTIGATION_PLANNING
        else "end"
    )


def _route_after_planning_guard(state: AgentState, terminal_report_enabled: bool = False) -> str:
    if state["evaluation_decision"] == EvaluationDecision.NEEDS_MANUAL_ACTION:
        return "manual_terminalization" if terminal_report_enabled else "end"
    return (
        "investigation_planning"
        if state["current_stage"] == AgentStage.INVESTIGATION_PLANNING
        else "end"
    )


def _route_after_planning(state: AgentState) -> str:
    return "tool_execution" if state["current_stage"] == AgentStage.TOOL_EXECUTION else "end"


def _route_after_tool_execution(state: AgentState) -> str:
    if state["current_stage"] == AgentStage.HYPOTHESIS_UPDATE:
        return "hypothesis_update"
    if state["current_stage"] == AgentStage.INVESTIGATION_PLANNING:
        return "planning_guard"
    return "end"


def _route_after_hypothesis_update(state: AgentState) -> str:
    return (
        "evidence_evaluation"
        if state["current_stage"] == AgentStage.EVIDENCE_EVALUATION
        else "end"
    )


def _route_after_evidence_evaluation(
    state: AgentState, terminal_report_enabled: bool = False
) -> str:
    if state["evaluation_decision"] == EvaluationDecision.CONTINUE:
        return "planning_guard"
    if state["evaluation_decision"] == EvaluationDecision.CONCLUDE:
        return "resolution_proposal"
    return "manual_terminalization" if terminal_report_enabled else "end"


def _route_after_policy_gate(state: AgentState, terminal_report_enabled: bool = False) -> str:
    outcome = state["policy_outcome"]
    if outcome is not None and outcome.decision is PolicyDecision.APPROVAL_REQUIRED:
        return "approval_wait"
    return "manual_terminalization" if terminal_report_enabled else "end"
