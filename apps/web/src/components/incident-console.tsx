"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  getFinalReport,
  getIncident,
  getWorkflow,
  retryWorkflow,
  startWorkflow,
  submitApproval,
} from "../lib/api";
import { formatDate, type ApprovalDecision, type FinalReport, type Incident, type WorkflowResponse } from "../lib/types";
import { FinalReportView } from "./final-report";
import { StatusBadge } from "./status-badge";
import { WorkflowView } from "./workflow-view";

interface IncidentConsoleProps {
  incidentId: string;
}

const terminalStatuses = new Set(["RESOLVED", "NEEDS_MANUAL_ACTION"]);

function isWorkflowNotStarted(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404 && error.detail === "Workflow not started";
}

function messageFor(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.detail : fallback;
}

export function IncidentConsole({ incidentId }: IncidentConsoleProps) {
  const [incident, setIncident] = useState<Incident | null>(null);
  const [workflow, setWorkflow] = useState<WorkflowResponse | null>(null);
  const [report, setReport] = useState<FinalReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [workflowLoading, setWorkflowLoading] = useState(false);
  const [mutationPending, setMutationPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [approvalRetryDecision, setApprovalRetryDecision] = useState<ApprovalDecision | null>(null);
  const [workflowError, setWorkflowError] = useState<string | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const refreshInFlight = useRef(false);
  const reportRequested = useRef(false);

  const refresh = useCallback(async () => {
    setWorkflowLoading(true);
    try {
      const nextIncident = await getIncident(incidentId);
      setIncident(nextIncident);
      setError(null);
      try {
        const nextWorkflow = await getWorkflow(incidentId);
        setWorkflow(nextWorkflow);
        setWorkflowError(null);
      } catch (workflowLoadError: unknown) {
        if (isWorkflowNotStarted(workflowLoadError)) {
          setWorkflow(null);
          setWorkflowError(null);
        } else {
          setWorkflowError(messageFor(workflowLoadError, "Unable to load the workflow."));
        }
      }
    } catch (incidentLoadError: unknown) {
      setIncident(null);
      setWorkflow(null);
      setReport(null);
      setApprovalRetryDecision(null);
      setWorkflowError(null);
      setReportError(null);
      setError(messageFor(incidentLoadError, "Unable to load the Incident."));
    } finally {
      setLoading(false);
      setWorkflowLoading(false);
    }
  }, [incidentId]);

  useEffect(() => {
    reportRequested.current = false;
    setIncident(null);
    setWorkflow(null);
    setReport(null);
    setError(null);
    setMutationError(null);
    setApprovalRetryDecision(null);
    setWorkflowError(null);
    setReportError(null);
    setLoading(true);
    void refresh();
  }, [incidentId, refresh]);

  useEffect(() => {
    if (
      !incident ||
      incident.status !== "INVESTIGATING" ||
      terminalStatuses.has(incident.status) ||
      mutationPending
    ) {
      return;
    }
    const interval = window.setInterval(() => {
      if (refreshInFlight.current) {
        return;
      }
      refreshInFlight.current = true;
      void refresh().finally(() => {
        refreshInFlight.current = false;
      });
    }, 2500);
    return () => window.clearInterval(interval);
  }, [incident, mutationPending, refresh, workflow]);

  const shouldFetchReport = Boolean(
    incident && (terminalStatuses.has(incident.status) || workflow?.report_outcome !== null && workflow?.report_outcome !== undefined),
  );

  useEffect(() => {
    if (!shouldFetchReport || reportRequested.current) {
      return;
    }
    reportRequested.current = true;
    void getFinalReport(incidentId)
      .then((nextReport) => {
        setReport(nextReport);
        setReportError(null);
      })
      .catch((reportLoadError: unknown) => {
        setReportError(
          reportLoadError instanceof ApiError && reportLoadError.status === 404
            ? "Final report is not available yet."
            : messageFor(reportLoadError, "Unable to load the Final Report."),
        );
      });
  }, [incidentId, shouldFetchReport]);

  async function startInvestigation() {
    setMutationPending(true);
    setMutationError(null);
    try {
      await startWorkflow(incidentId);
      setWorkflowError(null);
      await refresh();
    } catch (startError: unknown) {
      setMutationError(messageFor(startError, "Unable to start the workflow."));
      await refresh();
    } finally {
      setMutationPending(false);
    }
  }

  async function retryInvestigation() {
    setMutationPending(true);
    setMutationError(null);
    try {
      const retried = await retryWorkflow(incidentId);
      setWorkflow(retried);
      setWorkflowError(null);
      await refresh();
    } catch (retryError: unknown) {
      setMutationError(messageFor(retryError, "Unable to retry the investigation."));
      await refresh();
    } finally {
      setMutationPending(false);
    }
  }

  async function decideApproval(decision: ApprovalDecision) {
    setMutationPending(true);
    setMutationError(null);
    try {
      await submitApproval(incidentId, decision);
      setApprovalRetryDecision(null);
      await refresh();
    } catch (approvalError: unknown) {
      setMutationError(messageFor(approvalError, "Unable to record the Approval decision."));
      if (approvalError instanceof ApiError && approvalError.status === 503) {
        setApprovalRetryDecision(decision);
      }
      await refresh();
    } finally {
      setMutationPending(false);
    }
  }

  if (loading && incident === null) {
    return <main className="page-shell console-shell"><p className="empty-state">Loading Incident…</p></main>;
  }

  if (incident === null) {
    return (
      <main className="page-shell console-shell">
        <Link className="back-link" href="/">← All Incidents</Link>
        <p className="error-banner" role="alert">{error ?? "Incident is not available."}</p>
      </main>
    );
  }

  const canStart =
    error === null &&
    incident.status === "OPEN" &&
    workflow === null &&
    !workflowLoading &&
    workflowError === null;
  const retryEligibilityKnown =
    error === null &&
    incident.status === "INVESTIGATING" &&
    workflow?.retry_available === true &&
    !workflowLoading &&
    workflowError === null &&
    approvalRetryDecision === null;
  const canRetryInvestigation = retryEligibilityKnown && !mutationPending;
  const canApprove =
    error === null &&
    incident.status === "WAITING_APPROVAL" &&
    workflow?.current_stage === "waiting_approval" &&
    workflow.policy_outcome?.decision === "APPROVAL_REQUIRED" &&
    workflow.action !== null &&
    workflow.approval_outcome === null &&
    !workflowLoading &&
    workflowError === null &&
    approvalRetryDecision === null;

  return (
    <main className="page-shell console-shell">
      <Link className="back-link" href="/">← All Incidents</Link>
      <header className="console-header">
        <div>
          <p className="eyebrow">Incident Console</p>
          <h1>{incident.service}</h1>
          <p className="mono compact-id">{incident.id}</p>
        </div>
        <dl className="header-facts">
          <div><dt>Environment</dt><dd>{incident.environment}</dd></div>
          <div><dt>Incident Status</dt><dd><StatusBadge value={incident.status} /></dd></div>
          <div><dt>Agent Current Stage</dt><dd>{workflow?.current_stage ?? "Not started"}</dd></div>
        </dl>
      </header>

      {error ? <p className="error-banner" role="alert">{error}</p> : null}
      {mutationError ? <p className="error-banner" role="alert">{mutationError}</p> : null}
      {workflowError ? <p className="error-banner" role="alert">{workflowError}</p> : null}
      {reportError ? <p className="empty-state">{reportError}</p> : null}

      {canStart ? (
        <section className="panel start-panel">
          <div><p className="eyebrow">Workflow</p><h2>Investigation has not started</h2><p>Start the persisted workflow for this Incident.</p></div>
          <button className="button primary-button" disabled={mutationPending} onClick={() => void startInvestigation()} type="button">
            {mutationPending ? "Starting…" : "Start Investigation"}
          </button>
        </section>
      ) : null}

      {incident.status === "INVESTIGATING" && workflow === null ? (
        <section className="panel pending-workflow-panel">
          <p>Investigation accepted. Waiting for the first persisted workflow checkpoint…</p>
        </section>
      ) : null}

      {workflowLoading && workflow ? <p className="subtle-status">Refreshing workflow…</p> : null}
      {workflow ? (
        <>
          <WorkflowView workflow={workflow} />
          <section className="panel" aria-labelledby="decision-heading">
            <p className="eyebrow">Decision and action</p>
            <h2 id="decision-heading">Policy, Approval, and Recovery</h2>
            {retryEligibilityKnown ? (
              <div className="approval-controls">
                <p>Investigation execution was interrupted after its progress was persisted. Retry continues the same investigation thread.</p>
                <button className="button primary-button" disabled={!canRetryInvestigation} onClick={() => void retryInvestigation()} type="button">
                  {mutationPending ? "Retrying…" : "Retry Investigation"}
                </button>
              </div>
            ) : null}
            <div className="decision-grid">
              {workflow.proposed_action ? (
                <article className="record-card"><h3>Proposed Action</h3><p><strong>{workflow.proposed_action.action_type}</strong> — {workflow.proposed_action.summary}</p><p>{workflow.proposed_action.reason}</p><p>Risk: {workflow.proposed_action.risk}</p><p className="mono compact-id">Evidence: {workflow.proposed_action.supporting_evidence_ids.join(", ") || "—"}</p></article>
              ) : null}
              {workflow.policy_outcome ? (
                <article className="record-card"><h3>Policy</h3><p><StatusBadge value={workflow.policy_outcome.decision} /></p><p>{workflow.policy_outcome.reason_code}</p><p>{workflow.policy_outcome.reason}</p></article>
              ) : null}
              {workflow.action ? (
                <article className="record-card"><h3>Authoritative Action</h3><p className="mono compact-id">{workflow.action.action_id}</p><p><StatusBadge value={workflow.action.status} /></p><dl className="detail-grid"><div><dt>Service</dt><dd>{workflow.action.parameters.service}</dd></div><div><dt>Environment</dt><dd>{workflow.action.parameters.environment}</dd></div><div><dt>Current version</dt><dd>{workflow.action.parameters.current_version}</dd></div><div><dt>Target version</dt><dd>{workflow.action.parameters.target_version}</dd></div><div className="full-detail"><dt>Reason</dt><dd>{workflow.action.parameters.reason}</dd></div></dl></article>
              ) : null}
            </div>
            {canApprove ? (
              <div className="approval-controls"><p>Approve or reject the authoritative Action above. Action parameters cannot be edited here.</p><button className="button primary-button" disabled={mutationPending} onClick={() => void decideApproval("APPROVE")} type="button">Approve</button><button className="button danger-button" disabled={mutationPending} onClick={() => void decideApproval("REJECT")} type="button">Reject</button></div>
            ) : null}
            {approvalRetryDecision && error === null ? (
              <div className="approval-controls">
                <p>The approval decision may already be persisted, but workflow resume failed. Only the same decision can be retried.</p>
                <button className="button primary-button" disabled={mutationPending} onClick={() => void decideApproval(approvalRetryDecision)} type="button">
                  {mutationPending ? "Retrying…" : `Retry ${approvalRetryDecision === "APPROVE" ? "Approve" : "Reject"}`}
                </button>
              </div>
            ) : null}
            <div className="decision-grid">
              {workflow.approval_outcome ? <article className="record-card"><h3>Approval</h3><p><StatusBadge value={workflow.approval_outcome.status} /></p><p className="mono compact-id">{workflow.approval_outcome.approval_id}</p></article> : null}
              {workflow.execution_outcome ? <article className="record-card"><h3>Execution</h3><p><StatusBadge value={workflow.execution_outcome.status} /></p><p>{workflow.execution_outcome.service ?? "—"} / {workflow.execution_outcome.environment ?? "—"} → {workflow.execution_outcome.target_version ?? "—"}</p><p>Executed: {String(workflow.execution_outcome.executed)}</p></article> : null}
              {workflow.verification_outcome ? <article className="record-card"><h3>Recovery Verification</h3><p><StatusBadge value={workflow.verification_outcome.status} /></p><p>{workflow.verification_outcome.summary}</p><p className="mono compact-id">{workflow.verification_outcome.verification_id ?? "—"}</p></article> : null}
            </div>
          </section>
        </>
      ) : null}
      {report ? <FinalReportView report={report} /> : null}
      <footer className="console-footer">Created {formatDate(incident.created_at)} · Updated {formatDate(incident.updated_at)}</footer>
    </main>
  );
}
