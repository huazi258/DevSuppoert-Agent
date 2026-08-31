# V1 Eval Release Profile

`evals/initial_suite.yaml` remains the single source of truth for Fault Lab fixtures and their ground truth. `evals/v1_release_profiles.yaml` selects those fixtures without copying or changing them.

Each V1 release candidate must run the `p0_fault_lab` profile. It covers distinct engineering and safety boundaries: happy-path remediation, a second fault direction, investigation-tool failure, approval rejection, recovery-verification failure, and production policy denial. The selection is based on those boundaries, not historical pass rate.

`extended_fault_lab` retains the two wording variants for wording robustness and wider regression coverage. It is not a replacement for P0.

Real-integration acceptance is handled separately by M5.3. The full backend test suite is not an Eval release profile. This document defines suite selection only; it does not set thresholds or claim that the V1 Release Gate has passed.
