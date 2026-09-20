# V2 Adapter 集成验收

本验收只验证 V2 的只读 Logs + Metrics Adapter 链路：

```text
Incident（明确 Target + Service）
→ TargetConfig / ProviderConfig / BackendConfig
→ OpenSearch 或 Prometheus Adapter
→ query_logs / query_metrics
→ normalized Runtime Evidence
→ 当前 Round 的 V2 Report
```

不复用 V1 的 `RESOLVED`、`NEEDS_MANUAL_ACTION`、Action、Approval、Execution、Verification 或 remediation 语义，也不启用 traces。

## 确定性回归

```powershell
cd apps/backend
python -m pytest -q tests/test_m2_acceptance.py tests/test_v2_adapter_contracts.py tests/test_v2_otel_integration.py
```

这覆盖 Fault Lab 的正常 Logs / Metrics Evidence、provider unavailable 映射、无 Evidence 失败边界，以及 OTel Target 的 capability allowlist。Fault Lab 是确定性回归环境，不重新设计其场景。

## OTel Demo live run

使用 [OTEL_DEMO_REAL_INTEGRATION.md](OTEL_DEMO_REAL_INTEGRATION.md) 指定的 upstream `3.0.0` / `1755859a9de82c2e5e225be68abc401a5ebf2b4f` 环境。先在该 upstream 环境将既有 `paymentFailure` flag 临时设为 `100%`，发起至少一次 checkout，并在 `finally`/清理步骤恢复原始 flag 配置；随后解析运行时端口，启动 DevSupport PostgreSQL 并迁移。DevSupport 不管理该 Demo 的生命周期或 flag：

```powershell
$env:DEVSUPPORT_DATABASE_URL = "postgresql+psycopg://devsupport:devsupport@127.0.0.1:15432/devsupport"
$env:DEVSUPPORT_OTEL_DEMO_OPENSEARCH_URL = "http://localhost:<opensearch-port>"
$env:DEVSUPPORT_OTEL_DEMO_PROMETHEUS_URL = "http://localhost:<prometheus-port>"
$env:DEVSUPPORT_OTEL_UPSTREAM_RELEASE = "<verified upstream release>"
$env:DEVSUPPORT_OTEL_UPSTREAM_COMMIT = "<verified upstream commit>"
$env:DEVSUPPORT_OTEL_UPSTREAM_IDENTITY_VERIFIED = "true"
$env:DEVSUPPORT_OTEL_FAULT_NAME = "paymentFailure"
$env:DEVSUPPORT_OTEL_FAULT_ENABLED = "true"
$env:DEVSUPPORT_OTEL_FAULT_RESTORED = "true"
$env:DEVSUPPORT_OTEL_CHECKOUT_ATTEMPTS = "<observed checkout count>"
$env:DEVSUPPORT_OTEL_CHECKOUT_HTTP_STATUS_COUNTS = '<safe JSON status-count mapping>'
$env:DEVSUPPORT_OTEL_NON_2XX_OBSERVED = "true"
cd apps/backend
python -m alembic upgrade head
python -m devsupport_backend.evals.v2_otel_integration --output ..\..\evals\results\v2-otel-integration.json
```

这些 scenario 环境变量必须由本次 live orchestration 的显式观察填入：先以 upstream checkout 的 release/commit 校验 identity，再在 fault 开启期间统计 checkout HTTP status，最后确认恢复。runner 会将 expected upstream 与 observed/verified upstream 分开保存；它不会从代码的 pinned 值推断 observed identity。任一 scenario fact 缺失时为 `BLOCKED`。

端点只用于 backend deployment configuration：不会写入 TargetConfig、Tool arguments、Evidence、Report 或 artifact。该 adapter-only 验收刻意以 `INCONCLUSIVE` 结束并持久化当前 Round Report；它不调用 LLM 或 Embedding Provider，因此 artifact 会记录 `external_model_provider_status: not_invoked`，不会将 Adapter 连通误报为模型质量。若另行运行完整调查且外部模型 Provider 阻断质量判断，artifact 必须为 `BLOCKED` 并记录 `external_model_provider_status: blocked`，不能将其混同为 Adapter 失败。

## Artifact

`evals/results/v2-otel-integration.json` 只保存 expected/observed upstream identity、identity verified、fault name、enabled/restored、checkout attempts、HTTP status counts、non-2xx observation、Target/Service/Environment、两个 Adapter 状态、normalized evidence counts、安全终态与 terminal reason、Report 是否持久化、side-effect tool count、Provider/payload safety 和 `PASS` / `FAIL` / `BLOCKED`。

禁止保存 Provider URL、credential、完整 Provider payload、raw logs 或 Tool arguments。`CONCLUDED` 只有在 `concluded_grounding_verified=true` 时才可通过；缺少 OTel endpoint 或 Provider 不可用时必须输出 `BLOCKED`，不能伪造 `PASS`。
