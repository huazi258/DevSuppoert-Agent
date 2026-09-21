# V2 本地完整核心流程验收

本手册将现有的 OpenTelemetry Demo、V2 Logs / Metrics Adapter、V2 bootstrap 与浏览器验收流程组合为一个真实的本地验收环境。它**不**使用 `browser-acceptance-local`：该 Target 有意配置不可用模型且没有运行时 Adapter，只验证浏览器的安全失败和续轮路径。

本环境的调查目标为 `otel-demo-local-full-acceptance` / `checkout`（UI 显示名为 `OpenTelemetry Demo Local Full Acceptance`）。它由启动时的既有 bootstrap 写入数据库，因此可以用于全新 PostgreSQL；Target 和 Service 不需要手工 SQL。

## 运行架构与范围

```text
OpenTelemetry Demo checkout
  ├─ OpenSearch logs ──────┐
  ├─ Prometheus metrics ───┼─ V2 TargetAdapterResolver → Runtime Evidence
  └─ Jaeger traces ────────┘

OpenAI-compatible Embedding Provider → Markdown ingestion → KnowledgeDocument / Chunk → Citation
OpenAI-compatible LLM Provider → Hypothesis / planner / conclusion → V2 Final Report
PostgreSQL + Backend + Web → Incident / Round / persisted history
```

当前 V2 已实现并验收的 OpenTelemetry provider 是 OpenSearch Logs 与 Prometheus Metrics。Jaeger 必须随上游 Demo 启动以保持完整的 Demo telemetry 栈，但 V2 没有 Jaeger Trace Adapter；Target 不会错误地声明 `traces` 可用。新增 Trace Adapter 不属于本次环境整理的范围。

## 前置条件

- Docker Desktop / Docker Compose v2；
- 位于 `G:\codex-work\opentelemetry-demo` 的已固定 OpenTelemetry Demo `3.0.0` checkout（commit `1755859a9de82c2e5e225be68abc401a5ebf2b4f`）；
- 一个可从 Backend 容器访问的 OpenAI-compatible Embedding endpoint；
- 一个可从 Backend 容器访问的 OpenAI-compatible chat-completions endpoint。二者可以是同一 Provider，也可以分开；
- `.env` 中已经配置 PostgreSQL 密码与以下变量。不要把 Key 写进本仓库、Target 配置或数据库：

```text
EMBEDDING_MODEL=
EMBEDDING_BASE_URL=
EMBEDDING_API_KEY=
LLM_MODEL=
LLM_BASE_URL=
LLM_API_KEY=
LLM_TIMEOUT_SECONDS=50
```

Embedding client 调用 `<EMBEDDING_BASE_URL>/embeddings`，LLM client 调用 `<LLM_BASE_URL>/chat/completions`。因此 `*_BASE_URL` 必须是带 API 版本前缀的 OpenAI-compatible base URL，而不是某个模型容器的管理页面。

## 启动

先启动上游 Demo。V2 不管理其生命周期；必须从上游 checkout 使用其既有 Compose 层：

```powershell
Set-Location G:\codex-work\opentelemetry-demo
docker compose --env-file .env --env-file .env.override -f compose.yaml -f compose.observability.yaml -f compose.extras.yaml up --force-recreate --remove-orphans --detach
```

确认 `opensearch`、`prometheus`、`otel-collector`、`checkout`、`product-catalog` 与 `frontend-proxy` 均已正常运行，并制造 checkout 流量。当前主机的上游 Demo 已知需要将 `checkout` 与 `product-catalog` 的内存上限提高到 128 MiB；若端口 `8080` 已被占用，也必须先释放该端口或在上游环境中使用 operator-owned 的端口覆盖。两者都是上游 Demo 的运行前置条件，不是 DevSupport 配置。

然后在 DevSupport checkout 生成只含非 Secret endpoint、Target 与 Provider 引用的 overlay。脚本从上游 Compose 的实际端口映射取得 OpenSearch / Prometheus 地址，避免硬编码宿主机端口：

```powershell
Set-Location G:\codex-work\DevSuppoert Agent
.\scripts\New-V2LocalFullAcceptanceConfig.ps1 -OtelDemoPath G:\codex-work\opentelemetry-demo
docker compose -f docker-compose.yml -f docker-compose.local-full-acceptance.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.local-full-acceptance.yml ps
```

这会读取 `.env` 中的模型凭据，并由 `.env.local-full-acceptance` 覆盖 Target / Provider 注册。该生成文件被 Git 忽略，且不含模型 Key 或 Provider credential。Backend 启动顺序保持：`alembic upgrade head` → `python -m devsupport_backend.bootstrap` → API。

全新数据库也使用相同命令；不需要手工创建 Target 或 Service。已有数据库保留旧浏览器验收记录以保护历史，但 selector 只展示当前 overlay 中已配置的 `otel-demo-local-full-acceptance`。

## 验收顺序

在 Web (`http://127.0.0.1:3000`) 中：

1. `/knowledge` 选择 `OpenTelemetry Demo Local Full Acceptance`、`Checkout`、环境 `local`，上传 [otel-demo-checkout.md](../knowledge/architecture/otel-demo-checkout.md)。确认 201 响应、列表存在文档，并在 PostgreSQL 中有启用的 `KnowledgeDocument` 与 `KnowledgeChunk`。
2. 创建同一 Target / Service 的 Incident，时间范围覆盖刚生成的 checkout 流量。
3. 启动 Round 1，读取 workflow / timeline / report。成功调查必须包含 `search_knowledge` Citation、`query_logs` 和 `query_metrics` Runtime Evidence、至少一个 Hypothesis 与持久化 Final Report。
4. 从终态 Incident 提交新的 observation，验证 Round 2 保留 Round 1 的 Evidence 与 Report。

可复用的确定性与 live Adapter 检查：

```powershell
Set-Location apps\backend
uv run pytest -q tests/test_bootstrap.py tests/test_knowledge_api.py tests/test_m2_acceptance.py tests/test_v2_otel_integration.py tests/test_scoped_rag_acceptance.py tests/test_m4_workflow_reliability_acceptance.py

$env:DEVSUPPORT_RUN_OTEL_DEMO_ACCEPTANCE = "1"
$env:DEVSUPPORT_OTEL_DEMO_OPENSEARCH_URL = "http://127.0.0.1:<mapped-opensearch-port>"
$env:DEVSUPPORT_OTEL_DEMO_PROMETHEUS_URL = "http://127.0.0.1:<mapped-prometheus-port>"
uv run pytest -q tests/test_m2_acceptance.py -k live_otel_demo
```

`docs/V2_INTEGRATION_ACCEPTANCE.md` 的 `v2_otel_integration` runner 是 Adapter-only acceptance，故意不调用模型并以安全终态结束；它不能替代本手册的完整 LLM / RAG workflow acceptance。

## 当前配置诊断

本机当前运行的 `browser-acceptance-local` 配置为：

- `EMBEDDING_BASE_URL=http://host.docker.internal:18081/v1`，请求 `/embeddings` 得到 `ConnectError: [Errno 111] Connection refused`；该端口没有可监听的 Embedding 服务；
- `LLM_MODEL=browser-acceptance-unavailable-provider`，`LLM_BASE_URL=http://host.docker.internal:18082/v1`；这是浏览器安全失败验收的刻意不可用 Provider；
- Target 没有启用 Logs、Metrics、Traces 或 Deployment Facts，且没有 ProviderConfig / ProviderBackendConfig。

因此它无法成为完整成功调查的环境。配置可访问的模型 endpoint 与本文 overlay 后，Markdown ingestion 的 `503` 会在 Embedding 调用成功后恢复；不能仅靠数据库 bootstrap 修复该网络错误。
