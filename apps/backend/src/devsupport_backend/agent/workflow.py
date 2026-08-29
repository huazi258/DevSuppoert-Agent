"""LangGraph wiring and loop controls for the bounded investigation workflow."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.orm import Session

from devsupport_backend.agent.budget import (
    DEFAULT_INVESTIGATION_BUDGET,
    ActiveExecutionBudgetExceeded,
    InvestigationBudget,
    UsageAccountingLLMClient,
    active_execution_budget_exhausted,
    collect_llm_usage,
    current_active_execution_seconds,
)
from devsupport_backend.agent.evidence_evaluator import LLMEvidenceEvaluator
from devsupport_backend.agent.llm import LLMClient
from devsupport_backend.agent.nodes.hypothesis_generation import hypothesis_generation_node
from devsupport_backend.agent.nodes.hypothesis_update import hypothesis_update_node
from devsupport_backend.agent.nodes.intake import intake_node
from devsupport_backend.agent.nodes.planner import (
    deterministic_initial_evidence_plan,
    investigation_planner_node,
)
from devsupport_backend.agent.nodes.retrieval import retrieval_node
from devsupport_backend.agent.nodes.tool_execution import (
    ToolExecutionDependencies,
    tool_execution_node,
)
from devsupport_backend.agent.observability import (
    InvestigationNodeObserver,
    observe_investigation_node,
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

DEFAULT_MAX_INVESTIGATION_ROUNDS = DEFAULT_INVESTIGATION_BUDGET.max_rounds
DEFAULT_MAX_TOOL_CALLS = DEFAULT_INVESTIGATION_BUDGET.max_tool_calls
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
        InvestigationBudget(max_rounds=self.max_rounds, max_tool_calls=self.max_tool_calls)

    @classmethod
    def from_budget(cls, budget: InvestigationBudget) -> "InvestigationLoopLimits":
        """Preserve the existing loop-limit API while deriving it from one budget contract."""
        return cls(max_rounds=budget.max_rounds, max_tool_calls=budget.max_tool_calls)

    @property
    def budget(self) -> InvestigationBudget:
        """Project existing enforced dimensions into the unified budget contract."""
        return InvestigationBudget(max_rounds=self.max_rounds, max_tool_calls=self.max_tool_calls)


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
    budget: InvestigationBudget | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    observer: InvestigationNodeObserver | None = None,
) -> CompiledStateGraph:
    """Compile the bounded Day 3 graph with an optional externally owned checkpointer."""
    if limits is not None and budget is not None:
        raise ValueError("pass either limits or budget, not both")
    effective_budget = budget or (
        limits.budget if limits is not None else DEFAULT_INVESTIGATION_BUDGET
    )
    loop_limits = limits or InvestigationLoopLimits.from_budget(effective_budget)
    accounting_llm_client = UsageAccountingLLMClient(dependencies.llm_client)
    evaluator = dependencies.evaluator
    if isinstance(evaluator, LLMEvidenceEvaluator):
        evaluator = evaluator.with_llm_client(accounting_llm_client)
    dependencies = replace(
        dependencies,
        llm_client=accounting_llm_client,
        evaluator=evaluator,
    )
    graph = StateGraph(AgentState)

    graph.add_node(
        "intake", _investigation_node("intake", intake_node, effective_budget, observer)
    )
    graph.add_node(
        "retrieval",
        _investigation_node(
            "retrieval",
            lambda state: retrieval_node(state, dependencies.rag_service),
            effective_budget,
            observer,
        ),
    )
    graph.add_node(
        "hypothesis_generation",
        _investigation_node(
            "hypothesis_generation",
            _enforce_llm_budget_node(
                _account_llm_usage_node(
                    lambda state: hypothesis_generation_node(state, dependencies.llm_client)
                ),
                effective_budget,
            ),
            effective_budget,
            observer,
        ),
    )
    graph.add_node(
        "planning_guard",
        _investigation_node(
            "planning_guard",
            lambda state: _planning_guard_node(state, loop_limits),
            effective_budget,
            observer,
        ),
    )
    graph.add_node(
        "investigation_planning",
        _investigation_node(
            "investigation_planning",
            _account_llm_usage_node(
                lambda state: _investigation_planning_node(
                    state, dependencies.llm_client, effective_budget
                )
            ),
            effective_budget,
            observer,
        ),
    )
    graph.add_node(
        "tool_execution",
        _investigation_node(
            "tool_execution",
            lambda state: _tool_execution_with_initial_evidence_batch(
                state, dependencies.tool_execution
            ),
            effective_budget,
            observer,
        ),
    )
    graph.add_node(
        "hypothesis_update",
        _investigation_node(
            "hypothesis_update",
            _enforce_llm_budget_node(
                _account_llm_usage_node(
                    lambda state: _hypothesis_update_round_node(state, dependencies.llm_client)
                ),
                effective_budget,
            ),
            effective_budget,
            observer,
        ),
    )
    graph.add_node(
        "evidence_evaluation",
        _investigation_node(
            "evidence_evaluation",
            _enforce_llm_budget_node(
                _account_llm_usage_node(
                    lambda state: evidence_evaluation_node(
                        state, dependencies.evaluator, loop_limits
                    )
                ),
                effective_budget,
            ),
            effective_budget,
            observer,
        ),
    )
    graph.add_node(
        "resolution_proposal",
        _investigation_node(
            "resolution_proposal",
            _enforce_llm_budget_node(
                _account_llm_usage_node(
                    lambda state: resolution_proposal_node(state, dependencies.llm_client)
                ),
                effective_budget,
            ),
            effective_budget,
            observer,
        ),
    )
    graph.add_node(
        "policy_gate",
        observe_investigation_node(
            "policy_gate", lambda state: policy_gate_node(state, dependencies.policy_gate), observer
        ),
    )
    graph.add_node(
        "approval_wait",
        observe_investigation_node(
            "approval_wait",
            lambda state: approval_wait_node(state, dependencies.approval_wait),
            observer,
        ),
    )
    graph.add_node(
        "approval_interrupt",
        observe_investigation_node(
            "approval_interrupt",
            lambda state: approval_interrupt_node(state, dependencies.approval_wait),
            observer,
        ),
    )
    graph.add_node(
        "approval_decision",
        observe_investigation_node(
            "approval_decision",
            lambda state: approval_decision_node(state, dependencies.approval_decision),
            observer,
        ),
    )
    has_terminal_report = (
        dependencies.final_report is not None and dependencies.manual_terminalizer is not None
    )
    if has_terminal_report:
        graph.add_node(
            "manual_terminalization",
            observe_investigation_node(
                "manual_terminalization",
                lambda state: dependencies.manual_terminalizer.mark_needs_manual_action(state),
                observer,
            ),
        )
        if dependencies.action_execution is None:
            add_final_report_node(graph, dependencies.final_report, observer=observer)
        graph.add_edge("manual_terminalization", "final_report")
    else:
        # Keep route targets valid in pure Day 3 unit graphs without a report boundary.
        graph.add_node(
            "manual_terminalization",
            observe_investigation_node("manual_terminalization", lambda state: state, observer),
        )
        graph.add_edge("manual_terminalization", END)
    if dependencies.action_execution is not None:
        add_post_approval_continuation(
            graph,
            action_execution=dependencies.action_execution,
            recovery_verification=dependencies.recovery_verification,
            final_report=dependencies.final_report,
            observer=observer,
        )

    graph.add_edge(START, "intake")
    graph.add_conditional_edges(
        "intake",
        lambda state: _route_after_intake(state, has_terminal_report),
        {
            "retrieval": "retrieval",
            "manual_terminalization": "manual_terminalization",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "retrieval",
        lambda state: _route_after_retrieval(state, has_terminal_report),
        {
            "hypothesis_generation": "hypothesis_generation",
            "manual_terminalization": "manual_terminalization",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "hypothesis_generation",
        lambda state: _route_after_hypothesis_generation(state, has_terminal_report),
        {
            "planning_guard": "planning_guard",
            "manual_terminalization": "manual_terminalization",
            "end": END,
        },
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
        lambda state: _route_after_planning(state, has_terminal_report),
        {
            "tool_execution": "tool_execution",
            "manual_terminalization": "manual_terminalization",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "tool_execution",
        lambda state: _route_after_tool_execution(state, has_terminal_report),
        {
            "hypothesis_update": "hypothesis_update",
            "planning_guard": "planning_guard",
            "manual_terminalization": "manual_terminalization",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "hypothesis_update",
        lambda state: _route_after_hypothesis_update(state, has_terminal_report),
        {
            "evidence_evaluation": "evidence_evaluation",
            "manual_terminalization": "manual_terminalization",
            "end": END,
        },
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
    graph.add_conditional_edges(
        "resolution_proposal",
        lambda state: _route_after_resolution_proposal(state, has_terminal_report),
        {
            "policy_gate": "policy_gate",
            "manual_terminalization": "manual_terminalization",
            "end": END,
        },
    )
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
    budget: InvestigationBudget | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    observer: InvestigationNodeObserver | None = None,
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
        budget=budget,
        checkpointer=checkpointer,
        observer=observer,
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


def _investigation_planning_node(
    state: AgentState,
    llm_client: LLMClient,
    budget: InvestigationBudget,
) -> AgentState:
    """Use bounded first-pass evidence collection before falling back to the LLM planner."""
    initial_plan = deterministic_initial_evidence_plan(state)
    if initial_plan is None:
        if _llm_budget_exhausted(state, budget):
            return _llm_budget_exhausted_state(state)
        return investigation_planner_node(state, llm_client)
    return {
        **state,
        "current_goal": initial_plan.investigation_goal,
        "pending_tool_call": initial_plan,
        "current_stage": AgentStage.TOOL_EXECUTION,
    }


def _tool_execution_with_initial_evidence_batch(
    state: AgentState, dependencies: ToolExecutionDependencies
) -> AgentState:
    """Collect the complementary initial probe before spending an LLM update call."""
    updated = tool_execution_node(state, dependencies)
    if not _should_collect_complementary_initial_probe(updated):
        return updated
    return {**updated, "current_stage": AgentStage.INVESTIGATION_PLANNING}


def _should_collect_complementary_initial_probe(state: AgentState) -> bool:
    if state["current_stage"] is not AgentStage.HYPOTHESIS_UPDATE:
        return False
    history = state["tool_history"]
    if not history or history[0].tool_name.value != "search_knowledge":
        return False
    runtime_tools = [item.tool_name.value for item in history[1:]]
    return len(runtime_tools) == 1 and runtime_tools[0] in {"query_logs", "query_traces"}


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


def _investigation_node(
    node_name: str,
    node: Callable[[AgentState], AgentState],
    budget: InvestigationBudget,
    observer: InvestigationNodeObserver | None,
) -> Callable[[AgentState], AgentState]:
    """Apply one shared active-execution boundary around an investigation node."""
    return observe_investigation_node(
        node_name,
        _enforce_active_execution_budget_node(node, budget),
        observer,
    )


def _enforce_active_execution_budget_node(
    node: Callable[[AgentState], AgentState],
    budget: InvestigationBudget,
) -> Callable[[AgentState], AgentState]:
    """Persist active elapsed time and divert deadline-limited work to the manual path."""

    def budgeted_node(state: AgentState) -> AgentState:
        if active_execution_budget_exhausted(state, budget):
            return _active_execution_budget_exhausted_state(state)
        try:
            updated = node(state)
        except ActiveExecutionBudgetExceeded:
            return _active_execution_budget_exhausted_state(state)
        updated = {
            **updated,
            "active_execution_seconds": current_active_execution_seconds(updated),
        }
        if active_execution_budget_exhausted(updated, budget):
            return _active_execution_budget_exhausted_state(updated)
        return updated

    return budgeted_node


def _active_execution_budget_exhausted_state(state: AgentState) -> AgentState:
    return {
        **state,
        "active_execution_seconds": current_active_execution_seconds(state),
        "evaluation_decision": EvaluationDecision.NEEDS_MANUAL_ACTION,
    }


def _account_llm_usage_node(
    node: Callable[[AgentState], AgentState],
) -> Callable[[AgentState], AgentState]:
    """Persist successful node-local LLM attempts without changing provider or node contracts."""

    def accounted_node(state: AgentState) -> AgentState:
        with collect_llm_usage() as usage:
            updated = node(state)
        if usage.call_count == 0:
            return updated
        return {**updated, "llm_call_count": state.get("llm_call_count", 0) + usage.call_count}

    return accounted_node


def _enforce_llm_budget_node(
    node: Callable[[AgentState], AgentState],
    budget: InvestigationBudget,
) -> Callable[[AgentState], AgentState]:
    """Route exhausted LLM-backed nodes through the existing manual terminal path."""

    def budgeted_node(state: AgentState) -> AgentState:
        if _llm_budget_exhausted(state, budget):
            return _llm_budget_exhausted_state(state)
        return node(state)

    return budgeted_node


def _llm_budget_exhausted(state: AgentState, budget: InvestigationBudget) -> bool:
    limit = budget.max_llm_calls
    return limit is not None and state.get("llm_call_count", 0) >= limit


def _llm_budget_exhausted_state(state: AgentState) -> AgentState:
    return {**state, "evaluation_decision": EvaluationDecision.NEEDS_MANUAL_ACTION}


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


def _route_after_intake(state: AgentState, terminal_report_enabled: bool = False) -> str:
    if state["evaluation_decision"] == EvaluationDecision.NEEDS_MANUAL_ACTION:
        return "manual_terminalization" if terminal_report_enabled else "end"
    return "retrieval" if state["current_stage"] == AgentStage.RETRIEVAL else "end"


def _route_after_retrieval(state: AgentState, terminal_report_enabled: bool = False) -> str:
    if state["evaluation_decision"] == EvaluationDecision.NEEDS_MANUAL_ACTION:
        return "manual_terminalization" if terminal_report_enabled else "end"
    return (
        "hypothesis_generation"
        if state["current_stage"] == AgentStage.HYPOTHESIS_GENERATION
        else "end"
    )


def _route_after_hypothesis_generation(
    state: AgentState, terminal_report_enabled: bool = False
) -> str:
    if state["evaluation_decision"] == EvaluationDecision.NEEDS_MANUAL_ACTION:
        return "manual_terminalization" if terminal_report_enabled else "end"
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


def _route_after_planning(state: AgentState, terminal_report_enabled: bool = False) -> str:
    if state["evaluation_decision"] == EvaluationDecision.NEEDS_MANUAL_ACTION:
        return "manual_terminalization" if terminal_report_enabled else "end"
    return "tool_execution" if state["current_stage"] == AgentStage.TOOL_EXECUTION else "end"


def _route_after_tool_execution(state: AgentState, terminal_report_enabled: bool = False) -> str:
    if state["evaluation_decision"] == EvaluationDecision.NEEDS_MANUAL_ACTION:
        return "manual_terminalization" if terminal_report_enabled else "end"
    if state["current_stage"] == AgentStage.HYPOTHESIS_UPDATE:
        return "hypothesis_update"
    if state["current_stage"] == AgentStage.INVESTIGATION_PLANNING:
        return "planning_guard"
    return "end"


def _route_after_hypothesis_update(
    state: AgentState, terminal_report_enabled: bool = False
) -> str:
    if state["evaluation_decision"] == EvaluationDecision.NEEDS_MANUAL_ACTION:
        return "manual_terminalization" if terminal_report_enabled else "end"
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


def _route_after_resolution_proposal(
    state: AgentState, terminal_report_enabled: bool = False
) -> str:
    if state["evaluation_decision"] == EvaluationDecision.NEEDS_MANUAL_ACTION:
        return "manual_terminalization" if terminal_report_enabled else "end"
    return "policy_gate" if state["current_stage"] == AgentStage.CONCLUSION else "end"


def _route_after_policy_gate(state: AgentState, terminal_report_enabled: bool = False) -> str:
    outcome = state["policy_outcome"]
    if outcome is not None and outcome.decision is PolicyDecision.APPROVAL_REQUIRED:
        return "approval_wait"
    return "manual_terminalization" if terminal_report_enabled else "end"
