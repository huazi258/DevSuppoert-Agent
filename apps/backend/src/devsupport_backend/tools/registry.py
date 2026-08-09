"""Immutable whitelist of V0 tools and their Pydantic contracts."""

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from devsupport_backend.tools.schemas import (
    GetDeploymentHistoryInput,
    GetDeploymentHistoryOutput,
    QueryLogsInput,
    QueryLogsOutput,
    QueryMetricsInput,
    QueryMetricsOutput,
    QueryTracesInput,
    QueryTracesOutput,
    RollbackDeploymentInput,
    RollbackDeploymentOutput,
    SearchKnowledgeInput,
    SearchKnowledgeOutput,
)


class ToolName(StrEnum):
    """The only tool names that the V0 Agent may request."""

    SEARCH_KNOWLEDGE = "search_knowledge"
    QUERY_LOGS = "query_logs"
    QUERY_METRICS = "query_metrics"
    QUERY_TRACES = "query_traces"
    GET_DEPLOYMENT_HISTORY = "get_deployment_history"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"


class UnknownToolError(LookupError):
    """Raised when a caller requests a tool outside the fixed whitelist."""


@dataclass(frozen=True)
class ToolDefinition:
    """The public input/output contract for one allowed tool."""

    name: ToolName
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    implemented: bool


_DEFINITIONS = (
    ToolDefinition(
        ToolName.SEARCH_KNOWLEDGE,
        SearchKnowledgeInput,
        SearchKnowledgeOutput,
        implemented=True,
    ),
    ToolDefinition(ToolName.QUERY_LOGS, QueryLogsInput, QueryLogsOutput, implemented=True),
    ToolDefinition(
        ToolName.QUERY_METRICS,
        QueryMetricsInput,
        QueryMetricsOutput,
        implemented=True,
    ),
    ToolDefinition(ToolName.QUERY_TRACES, QueryTracesInput, QueryTracesOutput, implemented=True),
    ToolDefinition(
        ToolName.GET_DEPLOYMENT_HISTORY,
        GetDeploymentHistoryInput,
        GetDeploymentHistoryOutput,
        implemented=True,
    ),
    ToolDefinition(
        ToolName.ROLLBACK_DEPLOYMENT,
        RollbackDeploymentInput,
        RollbackDeploymentOutput,
        implemented=True,
    ),
)


class ToolRegistry:
    """Read-only lookup over the fixed V0 tool whitelist."""

    def __init__(self) -> None:
        self._definitions = {definition.name: definition for definition in _DEFINITIONS}

    def get(self, name: str | ToolName) -> ToolDefinition:
        """Return a registered tool definition or reject an unknown name."""
        try:
            tool_name = ToolName(name)
        except ValueError as error:
            raise UnknownToolError(f"tool is not registered: {name}") from error
        return self._definitions[tool_name]

    def list(self) -> tuple[ToolDefinition, ...]:
        """List allowed tools in their stable registry order."""
        return _DEFINITIONS


tool_registry = ToolRegistry()
