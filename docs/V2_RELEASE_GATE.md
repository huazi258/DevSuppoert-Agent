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

Gate 只有在全部八个 deterministic core cases 通过，并且 fake evidence、side-effect tool 与 scope leakage 均为 0 时才会 `PASS`。任何断言失败是 `FAIL`；测试运行器、测试选择或基础设施无法提供确定性证据时是 `BLOCKED`，不得被记作产品通过。
