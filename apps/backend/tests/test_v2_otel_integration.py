"""V2-only contracts for the safe OpenTelemetry Demo integration artifact."""

from __future__ import annotations

import json

from devsupport_backend.evals.v2_otel_integration import (
    AdapterAcceptanceStatus,
    V2AdapterAcceptance,
    V2IntegrationStatus,
    V2ModelProviderStatus,
    V2OtelFaultScenario,
    V2OtelIntegrationFacts,
    V2OtelObservedUpstream,
    V2OtelScenarioFacts,
    V2OtelTargetIdentity,
    V2ProviderPayloadSafety,
    assess_v2_otel_integration,
    write_v2_otel_integration_artifact,
)
from devsupport_backend.investigation_status import InvestigationStatus


def _facts() -> V2OtelIntegrationFacts:
    return V2OtelIntegrationFacts(
        scenario=V2OtelScenarioFacts(
            observed_upstream=V2OtelObservedUpstream(
                release="3.0.0",
                commit="1755859a9de82c2e5e225be68abc401a5ebf2b4f",
                identity_verified=True,
            ),
            fault=V2OtelFaultScenario(
                fault_name="paymentFailure",
                enabled=True,
                restored=True,
                checkout_attempts=5,
                checkout_http_status_counts={"500": 5},
                non_2xx_observed=True,
            ),
        ),
        target=V2OtelTargetIdentity(),
        available_tools=["query_logs", "query_metrics", "search_knowledge"],
        logs_adapter=V2AdapterAcceptance(
            status=AdapterAcceptanceStatus.SUCCESS,
            normalized_evidence_count=1,
            normalized_source="opensearch",
        ),
        metrics_adapter=V2AdapterAcceptance(
            status=AdapterAcceptanceStatus.SUCCESS,
            normalized_evidence_count=1,
            normalized_source="prometheus",
        ),
        normalized_runtime_evidence_count=2,
        final_status=InvestigationStatus.INCONCLUSIVE,
        terminal_reason="investigation_inconclusive",
        report_persisted=True,
        side_effect_tool_count=0,
        provider_payload_safety=V2ProviderPayloadSafety(),
    )


def test_v2_otel_integration_passes_with_two_real_adapter_evidence_projections() -> None:
    assessment = assess_v2_otel_integration(_facts())

    assert assessment.status is V2IntegrationStatus.PASS
    assert assessment.failed_checks == []


def test_v2_otel_integration_blocks_provider_connectivity_without_claiming_success() -> None:
    facts = _facts()
    facts.logs_adapter = V2AdapterAcceptance(
        status=AdapterAcceptanceStatus.BLOCKED,
        normalized_evidence_count=0,
        safe_error_code="provider_unavailable",
    )

    assessment = assess_v2_otel_integration(facts)

    assert assessment.status is V2IntegrationStatus.BLOCKED
    assert assessment.blockers == ["runtime_adapter_unavailable"]


def test_v2_otel_integration_blocks_when_live_scenario_facts_are_unavailable() -> None:
    facts = _facts()
    facts.scenario.fault.restored = None

    assessment = assess_v2_otel_integration(facts)

    assert assessment.status is V2IntegrationStatus.BLOCKED
    assert assessment.blockers == ["scenario_fault_state_unavailable"]


def test_v2_otel_integration_rejects_a_verified_but_wrong_upstream_identity() -> None:
    facts = _facts()
    facts.scenario.observed_upstream.commit = "different-commit"

    assessment = assess_v2_otel_integration(facts)

    assert assessment.status is V2IntegrationStatus.FAIL
    assert assessment.failed_checks == ["upstream_commit"]


def test_v2_otel_integration_rejects_incomplete_checkout_status_observation() -> None:
    facts = _facts()
    facts.scenario.fault.checkout_http_status_counts = {"500": 4}

    assessment = assess_v2_otel_integration(facts)

    assert assessment.status is V2IntegrationStatus.FAIL
    assert assessment.failed_checks == ["checkout_status_counts"]


def test_v2_otel_integration_separates_external_model_blockers_from_adapter_status() -> None:
    facts = _facts()
    facts.external_model_provider_status = V2ModelProviderStatus.BLOCKED

    assessment = assess_v2_otel_integration(facts)

    assert assessment.status is V2IntegrationStatus.BLOCKED
    assert assessment.blockers == ["external_model_provider_blocked"]


def test_v2_otel_integration_rejects_unsafe_or_ungrounded_claims() -> None:
    facts = _facts()
    facts.final_status = InvestigationStatus.CONCLUDED
    facts.concluded_grounding_verified = False
    facts.side_effect_tool_count = 1
    facts.provider_payload_safety = V2ProviderPayloadSafety(raw_provider_payload_exposed=True)

    assessment = assess_v2_otel_integration(facts)

    assert assessment.status is V2IntegrationStatus.FAIL
    assert set(assessment.failed_checks) == {
        "concluded_grounding",
        "provider_payload_safety",
        "side_effect_tools",
    }


def test_v2_otel_artifact_excludes_sensitive_values_and_tool_arguments(tmp_path) -> None:
    facts = _facts()
    path = tmp_path / "v2-otel-integration.json"
    write_v2_otel_integration_artifact(path, facts, assess_v2_otel_integration(facts))

    payload = path.read_text(encoding="utf-8")
    artifact = json.loads(payload)

    assert artifact["assessment"]["status"] == "PASS"
    assert artifact["integration"]["scenario"]["expected_upstream"]["release"] == "3.0.0"
    assert artifact["integration"]["scenario"]["observed_upstream"]["identity_verified"] is True
    assert artifact["integration"]["scenario"]["fault"]["checkout_http_status_counts"] == {
        "500": 5
    }
    assert "http://" not in payload
    assert '"credential":' not in payload
    assert '"raw_payload":' not in payload
    assert "tool_arguments" not in payload
