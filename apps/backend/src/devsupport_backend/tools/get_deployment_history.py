"""Structured Tool executor for current deployment facts."""

from time import perf_counter

from devsupport_backend.tools.adapter_contracts import (
    AdapterError,
    DeploymentAdapter,
    normalize_adapter_error,
)
from devsupport_backend.tools.schemas import (
    DeploymentRecord,
    GetDeploymentHistoryInput,
    GetDeploymentHistoryOutput,
    RuntimeEvidenceProvenance,
    ToolError,
    ToolStatus,
)


def get_deployment_history(
    tool_input: GetDeploymentHistoryInput,
    deployment_adapter: DeploymentAdapter,
) -> GetDeploymentHistoryOutput:
    """Return the current-plus-previous fact snapshot without inventing history entries."""
    started_at = perf_counter()
    try:
        result = deployment_adapter.query(tool_input)
    except AdapterError as error:
        normalized_error = normalize_adapter_error(error)
        return GetDeploymentHistoryOutput(
            status=ToolStatus.FAILURE,
            error=ToolError(**normalized_error.__dict__),
            duration_ms=_duration_ms(started_at),
        )

    return GetDeploymentHistoryOutput(
        status=ToolStatus.SUCCESS,
        duration_ms=_duration_ms(started_at),
        provenance=RuntimeEvidenceProvenance(
            source=result.provenance.source,
            service=tool_input.service,
            environment=tool_input.environment,
            observed_at=result.provenance.observed_at,
        ),
        deployments=[
            DeploymentRecord(
                service=result.service,
                environment=tool_input.environment,
                current_version=result.current_version,
                previous_version=result.previous_version,
                deployed_at=result.deployed_at,
            )
        ],
    )


def _duration_ms(started_at: float) -> float:
    """Return a non-negative elapsed duration for the future ToolCall audit."""
    return max(0.0, round((perf_counter() - started_at) * 1_000, 2))
