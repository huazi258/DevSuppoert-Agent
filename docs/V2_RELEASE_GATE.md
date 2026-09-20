# V2 Release Eval 与 Gate

V2 Release Gate 只评估只读调查能力，不复用 V1 的 Approval、Policy outcome、Action Execution、Recovery Verification 或 `RESOLVED` / `NEEDS_MANUAL_ACTION` 评分。

配置资产：

- `evals/v2_release_suite.yaml`：八个确定性 V2 core cases 与对应的聚焦测试契约；
- `evals/v2_release_gate.yaml`：不可放宽的 core case、证据、隔离、预算和只读阈值；
- `devsupport_backend.evals.v2_release`：独立 runner、结果 schema 与 gate assessor。

在 Backend 开发环境中运行：

```powershell
python -m devsupport_backend.evals.v2_release --output ..\..\evals\results\v2-release-gate.json
```

运行环境必须提供可迁移的测试 PostgreSQL，并通过 `DEVSUPPORT_DATABASE_URL` 指向它；suite 不调用 LLM、Embedding 或真实 Provider。输出是 machine-readable JSON，包含 `PASS`、`FAIL` 或 `BLOCKED`，以及 product failure、external provider blocked、eval infrastructure blocked 的独立列表。

`terminal_status_correctness`、terminal reason、citation、grounding 和 budget 字段是 **contract-verified facts**：runner 运行聚焦的确定性测试契约并消费其通过/失败结果；它们不表示 runner 直接采集了生产运行时 telemetry。

三个安全计数则必须由对应契约在实际运行时输出 `V2_RELEASE_FACT`：`provider_failure_retry_exhaustion` 输出 `fake_evidence_count`，`knowledge_isolation` 输出 `scope_leakage_count`，`read_only_safety` 输出 `side_effect_tool_count`。结果 JSON 中每项均包括 `expected_max`、实际 `observed` 与 `available`；阈值来自 gate policy，观察值只来自测试契约，绝不从 YAML expectation 或全量通过状态推断。任一所需事实缺失时，`observed` 为 `null`、`available` 为 `false`，Gate 必须为 `BLOCKED`。

Gate 只有在全部八个 deterministic core cases 通过，并且三个已验证安全计数均不超过阈值时才会 `PASS`。确定性产品契约失败是 `FAIL`；外部 provider、测试运行器、测试选择、基础设施或所需验证事实不可用时是 `BLOCKED`，不得被记作产品通过。
