"""Task 4.4 tests for the fixed local rollback Tool adapter."""

import json

import httpx
import pytest
from pydantic import ValidationError

from devsupport_backend.tools.deployments import FaultLabRollbackAdapter
from devsupport_backend.tools.rollback import rollback_deployment
from devsupport_backend.tools.schemas import RollbackDeploymentInput, ToolStatus


def _input(**updates: object) -> RollbackDeploymentInput:
    values: dict[str, object] = {
        "service": "order-service",
        "environment": "local",
        "target_version": "v1.0.0",
        "reason": "Verified deployment facts support rollback.",
        "approval_id": "b014bc6f-e406-49e4-a89f-7a5ca93ef061",
    }
    values.update(updates)
    return RollbackDeploymentInput.model_validate(values)


def test_rollback_adapter_posts_only_the_approved_target_to_fixed_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == httpx.URL("http://order-service.test/internal/deployment/rollback")
        assert json.loads(request.content) == {"target_version": "v1.0.0"}
        return httpx.Response(
            200,
            json={
                "service": "order-service",
                "current_version": "v1.0.0",
                "previous_version": "v1.1.0",
                "deployed_at": "2026-01-01T00:00:00Z",
                "target_version": "v1.0.0",
                "executed": True,
            },
        )

    adapter = FaultLabRollbackAdapter(
        order_service_url="http://order-service.test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    output = rollback_deployment(_input(), adapter)

    assert output.status is ToolStatus.SUCCESS
    assert output.executed is True


@pytest.mark.parametrize(
    "updates", [{"environment": "staging"}, {"service": "payment-service"}]
)
def test_rollback_adapter_rejects_non_local_or_non_order_service_without_http(
    updates: dict[str, str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected rollback request: {request.url}")

    adapter = FaultLabRollbackAdapter(
        order_service_url="http://order-service.test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    output = rollback_deployment(_input(**updates), adapter)

    assert output.status is ToolStatus.FAILURE
    assert output.executed is False


def test_rollback_input_requires_an_approval_id() -> None:
    with pytest.raises(ValidationError):
        RollbackDeploymentInput(
            service="order-service",
            environment="local",
            target_version="v1.0.0",
            reason="Verified deployment facts support rollback.",
        )
