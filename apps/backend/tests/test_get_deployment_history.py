from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from devsupport_backend.tools.deployments import FaultLabDeploymentAdapter
from devsupport_backend.tools.get_deployment_history import get_deployment_history
from devsupport_backend.tools.schemas import GetDeploymentHistoryInput, ToolStatus


def _adapter(handler: httpx.MockTransport) -> FaultLabDeploymentAdapter:
    return FaultLabDeploymentAdapter(
        order_service_url="http://order-service.test",
        payment_service_url="http://payment-service.test",
        http_client=httpx.Client(transport=handler, timeout=1.0),
    )


def test_get_deployment_history_returns_only_real_current_and_previous_facts() -> None:
    deployed_at = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://order-service.test/internal/deployment")
        return httpx.Response(
            200,
            json={
                "service": "order-service",
                "current_version": "v1.1.0",
                "previous_version": "v1.0.0",
                "deployed_at": deployed_at.isoformat(),
            },
        )

    output = get_deployment_history(
        GetDeploymentHistoryInput(service="order-service", environment="local"),
        _adapter(httpx.MockTransport(handler)),
    )

    assert output.status is ToolStatus.SUCCESS
    assert output.error is None
    assert len(output.deployments) == 1
    assert output.deployments[0].model_dump() == {
        "service": "order-service",
        "environment": "local",
        "current_version": "v1.1.0",
        "previous_version": "v1.0.0",
        "deployed_at": deployed_at,
    }
    assert not {"fault_name", "root_cause", "expected_answer", "recommended_action"} & set(
        output.model_dump()
    )


@pytest.mark.parametrize(
    ("tool_input", "error_code"),
    [
        (
            GetDeploymentHistoryInput(service="unknown-service", environment="local"),
            "unsupported_service",
        ),
        (
            GetDeploymentHistoryInput(service="order-service", environment="staging"),
            "unsupported_environment",
        ),
    ],
)
def test_get_deployment_history_rejects_non_whitelisted_fault_lab_targets(
    tool_input: GetDeploymentHistoryInput,
    error_code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"adapter should not request {request.url}")

    output = get_deployment_history(tool_input, _adapter(httpx.MockTransport(handler)))

    assert output.status is ToolStatus.FAILURE
    assert output.error is not None
    assert output.error.code == error_code
    assert output.deployments == []


def test_get_deployment_history_rejects_service_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "service": "payment-service",
                "current_version": "v1.0.0",
                "previous_version": None,
                "deployed_at": None,
            },
        )

    output = get_deployment_history(
        GetDeploymentHistoryInput(service="order-service", environment="local"),
        _adapter(httpx.MockTransport(handler)),
    )

    assert output.status is ToolStatus.FAILURE
    assert output.error is not None
    assert output.error.code == "service_mismatch"


def test_get_deployment_history_returns_structured_failure_when_fault_lab_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    output = get_deployment_history(
        GetDeploymentHistoryInput(service="payment-service", environment="local"),
        _adapter(httpx.MockTransport(handler)),
    )

    assert output.status is ToolStatus.FAILURE
    assert output.error is not None
    assert output.error.code == "fault_lab_unavailable"
    assert output.error.retryable
