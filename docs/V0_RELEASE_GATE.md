# V0 Release Gate

## Decision

**NOT_RELEASE_READY** — assessed from the committed Day 5 baseline results at
`e51acc0` and the regression commands below. Passing infrastructure or unit tests alone
does not override failed real-eval quality gates.

## Reproduce the checks

Run from the repository root after configuring the local providers and starting the Fault Lab
as described in [WEB_E2E_RUNBOOK.md](WEB_E2E_RUNBOOK.md):

```powershell
cd apps/backend
python -m pytest -q
python -m ruff check src tests
python -m devsupport_backend.evals.runner

cd ../../services/order-service
python -m pytest -q
python -m ruff check src tests

cd ../payment-service
python -m pytest -q
python -m ruff check src tests

cd ../../apps/web
npm run lint
npx tsc --noEmit
npm run build
```

The eval runner writes machine-readable JSON to stdout. Preserve a new baseline under
`evals/results/` before changing this decision. The fixture suite and its leakage checks are
part of `apps/backend/tests/test_eval_contract.py`, `test_eval_fixture_suite.py`, and
`test_eval_runner.py`.

## Gate checklist

| Area | Check | Evidence / current outcome |
| --- | --- | --- |
| Engineering | Backend pytest and Ruff | PASS in this release-gate verification. |
| Engineering | order-service and payment-service tests | PASS in this release-gate verification. |
| Engineering | Web lint, TypeScript check, and production build | PASS in this release-gate verification. |
| Eval | At least eight fixed fixtures | PASS: 8 fixtures, 7 `full_workflow` + 1 `policy_gate_safety`. |
| Eval | Automated execution and machine scoring | PASS: `python -m devsupport_backend.evals.runner`; committed JSON baselines. |
| Eval safety | Policy Safety Pass Rate = 100% | PASS in both committed baselines. |
| Eval safety | Unauthorized executions = 0 with complete metrics | PASS in both committed baselines. |
| Eval integrity | Expected truth is isolated from Agent input | PASS: contract, fixture-suite, and runner tests assert input-only Incident fields and symptom-only descriptions. |
| Functional | Scenario A approve reaches `RESOLVED` through same-thread approval, rollback, and verification | NOT DEMONSTRATED by current full baseline; the approve case timed out. Component tests cover approval resume, controlled execution, and verification PASS separately. |
| Functional | Scenario A reject reaches `NEEDS_MANUAL_ACTION` without rollback | PASS at workflow/API test level; no full baseline completion because its case timed out. |
| Functional | Scenario B does not rollback order-service | PASS at policy/fixture and evaluator-contract level; no completed Scenario B runtime baseline. |
| Functional | Verification is required for `RESOLVED`; FAIL/INCONCLUSIVE stays manual | PASS in recovery-verification tests and workflow contracts. |
| Quality | Root Cause Accuracy supports reliable V0 diagnosis | FAIL: 0.0% before and after hardening. |
| Quality | Full-workflow completion is reliable | FAIL: 6 of 7 full-workflow cases timeout/error after hardening. |

## Blocking reasons

1. Root Cause Accuracy is 0.0% in the after-hardening baseline.
2. Six of seven full-workflow cases still time out or error; the Scenario A approve E2E path
   did not reach `RESOLVED` in the recorded run.
3. LLM completion latency consumes most of the 120-second case deadline, preventing reliable
   workflow completion.
4. The recovery-verification failure case did not reach its required approval checkpoint.

## Safety posture

This is a quality-blocked release, not a safety-pass workaround. The production Policy Gate
case is `DENIED`; rollback remains the sole side effect, backend approval remains mandatory,
and verification remains the sole route to `RESOLVED`. No gate threshold was relaxed, no
timeout was raised, and no failing fixture was removed to obtain this decision.

For exact aggregate values and per-case facts, see [EVAL_RESULTS.md](EVAL_RESULTS.md).
