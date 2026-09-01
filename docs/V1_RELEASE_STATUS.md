# V1 Release Status

## Positioning

V1 is the scoped DevSupport AI Investigator release: a stateful, evidence-grounded microservice investigation workflow with controlled approval, rollback, and recovery boundaries.

## Verification

- Verified production commit: `6aa51676f24e4772b0958e31dde1d112128da957`
- Verification continuation head: `83a52b97299a10f09eba717ed8cfe13b63962760`
- Release status: **BLOCKED**
- Backend full suite: PASS (628 tests); Ruff: PASS.
- Web lint, TypeScript check, and production build: PASS.
- M5.3 real-integration acceptance: PASS after strict saved-artifact validation.
- M5.4 approval/recovery regression: PASS after strict saved-artifact validation; all three cases passed and Unauthorized Execution total was zero.

The resumed required current eight-case real-corpus RAG regression did not produce a complete benchmark result. Citation/retrieval release evidence therefore remains incomplete, neither P0 Fault Lab run was started, and the consecutive-clean-P0 count is zero. This is a BLOCKED release, not a product correctness PASS or FAIL.

See [the final verification manifest](../evals/results/v1-m5.5-release-verification.json) for the complete safe evidence summary.

## Known Limitations

- OpenTelemetry Demo local setup requires an external untracked 128MiB checkout/product-catalog Compose override.
- FastAPI BackgroundTasks are not a durable job queue if the process exits after HTTP 202 and before the first checkpoint.
- OpenTelemetry Demo currently integrates Logs and Metrics only; Traces remain skipped bonus scope.
- The latest real-integration PASS supports provider-neutral Knowledge + Logs + Metrics for a payment/downstream SUPPORTED hypothesis; it does not prove payment-scoped-metrics causal localization.
- External LLM-provider latency tails remain possible; the intended release-stability proof is two clean deterministic Fault Lab P0 runs, which are not available for this blocked verification.
