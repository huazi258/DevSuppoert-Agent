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
| Prometheus | Runtime port must be resolved with `docker compose port prometheus 9090`; the M5.3 host used its resolved `http://localhost:6990` endpoint because Windows reserved the default 9090 range. Service label is `service_name`; span metrics use the `traces_span_metrics_duration_milliseconds_*` family. |
| OpenSearch | Runtime port must be resolved with `docker compose port opensearch 9200` (M5.3 observed local endpoint: `http://localhost:6984`). Logs are in `otel-logs-*`; relevant fields are `@timestamp`, `body`, `severity`, `resource`, and `attributes`, with service at `resource.service.name`. |
| Jaeger | Runtime port must be resolved with `docker compose port jaeger 16686` (observed local endpoint: `http://localhost:1411/jaeger/ui`). Query the UI-prefixed API, including `/jaeger/ui/api/services` and `/jaeger/ui/api/traces`. |

M3.4 established only the environment and telemetry shape; M3.5 adds the logs-provider boundary below without switching the production workflow provider.

## Logs Adapter

`OpenSearchLogsAdapter` reads the runtime-resolved `OPENSEARCH_URL` and searches `otel-logs-*` with bounded JSON DSL. It maps `@timestamp`, `body`, `severity.text`, `resource["service.name"]`, and optional `traceId` into the existing normalized LogsAdapter contract. The OpenSearch request filters `resource.service.name`, inclusive `@timestamp`, optional severity/query values, and the Tool limit; it does not expose provider hit metadata or raw documents.

## Metrics Adapter

`PrometheusMetricsAdapter` reads the runtime-resolved `PROMETHEUS_URL`. It uses cumulative instant `traces_span_metrics_calls_total` and `traces_span_metrics_duration_milliseconds_{sum,count}` values, scoped to `service_name` and `span_kind="SPAN_KIND_SERVER"`; `STATUS_CODE_ERROR` is the failure subset. `target_info` establishes telemetry presence only. It deliberately returns `health_status="unknown"`, because span metrics are not a health probe, and `last_request_duration_ms=None`, because an aggregate histogram cannot identify the last request duration.

## End-to-End Acceptance

M3.7 exercised the production workflow with `runtime_evidence_provider=otel_demo` against a hidden, temporary payment-failure scenario. The workflow retrieved cited Checkout/shared knowledge and successfully recorded `query_logs` and `query_metrics` Runtime Evidence; it did not expose traces, deployment history, approval, or remediation. The first `hypothesis_update` reached the configured external LLM provider timeout, and its one authorized retry consumed the remaining active-execution budget before a hypothesis update could be persisted. The workflow terminalized safely with a persisted manual-action report and no Action or Approval. The complete non-sensitive result is [v1-m3.7-real-investigation-acceptance.json](../evals/results/v1-m3.7-real-investigation-acceptance.json). This provider-latency limitation prevented a SUPPORTED/CONFIRMED hypothesis in this run; it did not alter the Logs + Metrics provider boundary.

M5.3 adds the evaluator-only policy at `evals/v1_real_integration_acceptance.yaml`. Its one live `otel_payment_failure` run used the same production workflow with the `otel_demo` Logs + Metrics boundary, ten failed checkout requests, and a verified healthy checkout after the external fault flag was restored. It recorded successful knowledge, logs, and metrics Tool calls and no remediation path, but the payment/downstream hypotheses remained `ACTIVE` while a different aggregate-metrics hypothesis became `SUPPORTED`. The evaluator therefore classified the attempt as `FAIL`, not `BLOCKED`: no typed external LLM-provider interruption was recorded before the investigation-quality requirement was missed. The complete safe projection is [v1-m5.3-real-integration-acceptance.json](../evals/results/v1-m5.3-real-integration-acceptance.json).
