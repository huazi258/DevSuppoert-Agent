# M2.4 Adapter Runtime Acceptance

M2.4 verifies the formal V2 target-aware runtime boundary:

```text
Incident Target
→ TargetConfigRegistry
→ TargetAdapterResolver
→ ToolExecutionDependencies
→ read-only Tool
→ normalized Evidence
```

The acceptance tests do not directly instantiate an Adapter. Provider transport doubles are used only to make the deterministic tests repeatable; adapter selection and Tool execution always use the production resolver and executor.

## Automated deterministic acceptance

From `apps/backend`:

```powershell
uv run pytest -q tests/test_m2_acceptance.py
```

The Fault Lab Scenario A (`missing_config`) test configures all four capabilities as `fault_lab` and verifies normalized logs, metrics, traces, and deployment Evidence. It expects the existing scenario facts: `MissingRequiredConfiguration`, one failed request, a trace, and deployment version `v1.1.0`.

The OpenTelemetry Demo configuration test configures `logs=opensearch`, `metrics=prometheus`, and leaves traces and deployment facts unavailable. It verifies normalized logs and metrics, rejects disabled traces at the Tool boundary, and verifies an unavailable OpenSearch provider produces a structured failure with no Evidence.

## Start Fault Lab

Start each service in its own terminal:

```powershell
cd services/payment-service
uv run uvicorn payment_service.main:app --host 127.0.0.1 --port 8001
```

```powershell
cd services/order-service
uv run uvicorn order_service.main:app --host 127.0.0.1 --port 8000
```

The deterministic acceptance test is self-contained and does not require those processes. Start Fault Lab when manually inspecting its local endpoints or running the existing live Fault Lab evaluation workflow.

## Start OpenTelemetry Demo and run live acceptance

Use the pinned OpenTelemetry Demo 3.0.0 checkout described in [OTEL_DEMO_REAL_INTEGRATION.md](OTEL_DEMO_REAL_INTEGRATION.md), including its local capacity override when required:

```powershell
docker compose --env-file .env --env-file .env.override -f compose.yaml -f compose.observability.yaml -f compose.extras.yaml up --force-recreate --remove-orphans --detach
```

Generate recent `checkout` traffic before the test. Resolve the mapped endpoints from that checkout, then run:

```powershell
$env:DEVSUPPORT_RUN_OTEL_DEMO_ACCEPTANCE = "1"
$env:DEVSUPPORT_OTEL_DEMO_OPENSEARCH_URL = "http://localhost:<opensearch-port>"
$env:DEVSUPPORT_OTEL_DEMO_PROMETHEUS_URL = "http://localhost:<prometheus-port>"
cd apps/backend
uv run pytest -q tests/test_m2_acceptance.py -k live_otel_demo
```

The live test is skipped unless explicitly enabled. Success requires a successful `query_logs` result with at least one recent `checkout` log and a successful `query_metrics` result with a positive request count. It uses only deployment configuration references and does not expose URLs or credentials in Tool inputs or Evidence.

Stop the demo from its upstream checkout with the corresponding layered `docker compose ... down` command. DevSupport does not manage the demo lifecycle.
