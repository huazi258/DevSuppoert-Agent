# Eval Results — Day 5 Baseline

## Source and scope

This record summarizes the committed machine-readable runs, not a reconstructed or
hand-entered score:

- [Before hardening](../evals/results/baseline-before-hardening.json)
- [After hardening](../evals/results/baseline-after-hardening.json)

The fixed suite has eight fixtures: seven `full_workflow` cases and one
`policy_gate_safety` case. The full-workflow cases cover Scenario A approval and reject,
Scenario A tool and verification failure, and Scenario B standard and wording variants.
The policy-only case exercises production rollback denial without accessing Fault Lab
investigation adapters.

## Aggregate comparison

| Metric | Before hardening | After hardening |
| --- | ---: | ---: |
| Root Cause Accuracy | 0.0% | 0.0% |
| Key Evidence Recall | 0.0% | 7.1% |
| Tool Selection Accuracy | 0.0% | 14.3% |
| Task Completion Rate | 0.0% | 14.3% |
| Approval Trigger Accuracy | 0.0% | 14.3% |
| Policy Safety Pass Rate | 100.0% | 100.0% |
| Unauthorized Execution Count | 0 | 0 |
| Unauthorized-execution metrics complete | yes | yes |
| Average Tool Calls | 2.0 | 3.0 |
| Average Latency | 113.1 s | 117.3 s |
| LLM Call Count | 18 | 17 |
| Average LLM Calls / full-workflow case | 2.57 | 2.43 |
| Timeout or error cases | 7 | 6 |

Token usage is `null` in both runs because the provider boundary does not expose reliable
token usage. No token count has been invented.

## Observations

- The production Policy Gate case passed with `DENIED`; it has no Fault Lab investigation
  calls and did not create a side effect.
- All observed full-workflow unauthorized-execution facts were zero and complete.
- The after run completed the forced `query_logs` failure case as `NEEDS_MANUAL_ACTION`;
  its score still failed because root-cause and required-evidence expectations were not met.
- Six of seven after-hardening full-workflow cases still timed out or errored. No Scenario A
  approval-to-`RESOLVED` run completed in the committed baseline.

## Latency finding

LLM completion latency dominates the 120-second case budget. In the after-hardening baseline,
individual full-workflow cases recorded two or three completed LLM calls consuming roughly
63–95 seconds before timeout/error handling. The baseline therefore does not demonstrate a
reliable end-to-end workflow completion rate.

## Hardening represented by the after run

The preceding hardening run made three evidence-driven changes:

1. collected a bounded pair of complementary initial runtime probes before spending an
   intermediate planner/update round-trip;
2. resolved only unambiguous UUID prefixes already present in Agent state, fixing a real
   structured-output validation failure; and
3. clarified that confirmation needs a specific direct runtime fact plus corroboration.

These changes are generic workflow behavior, not fixture-ID logic or evaluator relaxation.
They improved evidence/tool/task metrics for one case but did not improve Root Cause Accuracy
or establish release readiness.

## Remaining failures

The current principal blockers are LLM/provider latency and incomplete full-workflow
completion. The recovery-verification fixture also failed before reaching its expected
approval checkpoint. These results are retained as failures; fixtures and scorer criteria
were not removed or weakened.
