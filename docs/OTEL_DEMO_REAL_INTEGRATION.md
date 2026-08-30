# OpenTelemetry Demo real integration environment

## Pinned upstream environment

The real-integration environment is [OpenTelemetry Demo 3.0.0](https://github.com/open-telemetry/opentelemetry-demo/tree/3.0.0), pinned to commit `1755859a9de82c2e5e225be68abc401a5ebf2b4f`.

It is started from the upstream checkout with its minimal, observability, and extras Compose layers:

```powershell
docker compose --env-file .env --env-file .env.override -f compose.yaml -f compose.observability.yaml -f compose.extras.yaml up --force-recreate --remove-orphans --detach
```

Stop the same layered environment with `docker compose ... down`; do not use DevSupport to manage the demo lifecycle.

## Local capacity finding

The upstream 20 MiB limits for `checkout` and `product-catalog` were not functional on this host. `product-catalog` was Docker-unhealthy, its direct gRPC health probe failed, and three deterministic checkout requests returned HTTP 504. Neither container was OOM-killed, so this is a local resource-capacity finding rather than an OOM diagnosis.

A temporary, untracked external Compose override raised only those two limits to 128 MiB. With that override, both containers became healthy, all manual gRPC probes returned `SERVING`, and three deterministic cart-plus-checkout transactions returned HTTP 200. The override is an external environment prerequisite for this host; it is not a DevSupport configuration or source change.

## Verified telemetry contract

The deterministic checkout path is the upstream telemetry-test flow: add one catalog item through the frontend proxy, then submit checkout. It produced checkout and payment spans in the same Jaeger trace shortly afterwards.

| Backend | Runtime contract |
| --- | --- |
| Prometheus | Available at `http://localhost:9090`; service label is `service_name`; span metrics use the `traces_span_metrics_duration_milliseconds_*` family. |
| OpenSearch | Runtime port must be resolved with `docker compose port opensearch 9200` (observed local endpoint: `http://localhost:1406`). Logs are in `otel-logs-*`; relevant fields are `@timestamp`, `body`, `severity`, `resource`, and `attributes`, with service at `resource.service.name`. |
| Jaeger | Runtime port must be resolved with `docker compose port jaeger 16686` (observed local endpoint: `http://localhost:1411/jaeger/ui`). Query the UI-prefixed API, including `/jaeger/ui/api/services` and `/jaeger/ui/api/traces`. |

This task only establishes the environment and its telemetry shape. It does not add a DevSupport adapter or query implementation.
