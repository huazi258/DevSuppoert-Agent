"""Structured implementation of the allowlisted rollback_deployment Tool."""

from devsupport_backend.tools.deployments import DeploymentAdapterError, FaultLabRollbackAdapter
from devsupport_backend.tools.schemas import (
    RollbackDeploymentInput,
    RollbackDeploymentOutput,
    ToolError,
    ToolStatus,
)


def rollback_deployment(
    tool_input: RollbackDeploymentInput, adapter: FaultLabRollbackAdapter
) -> RollbackDeploymentOutput:
    """Execute only a caller-validated Action through the fixed rollback adapter."""
    try:
        result = adapter.execute(tool_input)
    except DeploymentAdapterError as error:
        return RollbackDeploymentOutput(
            status=ToolStatus.UNAVAILABLE if error.retryable else ToolStatus.FAILURE,
            error=ToolError(code=error.code, message=str(error), retryable=error.retryable),
            service=tool_input.service,
            environment=tool_input.environment,
            target_version=tool_input.target_version,
            executed=False,
        )
    return RollbackDeploymentOutput(
        status=ToolStatus.SUCCESS,
        service=result.service,
        environment=result.environment,
        target_version=result.target_version,
        executed=result.executed,
    )
