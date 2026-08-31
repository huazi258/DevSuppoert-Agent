import type { WorkflowProgressResponse, WorkflowResponse } from "../lib/types";
import { StatusBadge } from "./status-badge";

interface InvestigationTechnicalDetailsProps {
  progress: WorkflowProgressResponse | null;
  workflow: WorkflowResponse | null;
}

interface AuditFact {
  label: string;
  value: string;
}

function yesNo(value: boolean): string {
  return value ? "Yes" : "No";
}

function duration(value: number | null): string {
  return value === null ? "Not recorded" : `${value.toFixed(0)} ms`;
}

function auditFacts(workflow: WorkflowResponse): AuditFact[] {
  return [
    workflow.policy_outcome ? { label: "Policy reason code", value: workflow.policy_outcome.reason_code } : null,
    workflow.policy_outcome?.action_id ? { label: "Policy action ID", value: workflow.policy_outcome.action_id } : null,
    workflow.action ? { label: "Action ID", value: workflow.action.action_id } : null,
    workflow.proposed_action && workflow.proposed_action.supporting_evidence_ids.length > 0
      ? { label: "Proposed supporting evidence IDs", value: workflow.proposed_action.supporting_evidence_ids.join(", ") }
      : null,
    workflow.approval_outcome ? { label: "Approval ID", value: workflow.approval_outcome.approval_id } : null,
    workflow.execution_outcome?.action_id ? { label: "Execution action ID", value: workflow.execution_outcome.action_id } : null,
    workflow.execution_outcome?.approval_id ? { label: "Execution approval ID", value: workflow.execution_outcome.approval_id } : null,
    workflow.verification_outcome?.verification_id ? { label: "Verification ID", value: workflow.verification_outcome.verification_id } : null,
    workflow.report_outcome ? { label: "Report ID", value: workflow.report_outcome.report_id } : null,
  ].filter((fact): fact is AuditFact => fact !== null);
}

function ToolCalls({ workflow }: { workflow: WorkflowResponse }) {
  return (
    <section className="technical-section" aria-labelledby="technical-tool-calls-heading">
      <h3 id="technical-tool-calls-heading">Tool calls</h3>
      {workflow.tool_history.length === 0 ? <p className="empty-state">No tool calls recorded.</p> : (
        <ol className="technical-tool-list">
          {workflow.tool_history.map((tool, index) => (
            <li className="technical-tool-call" key={`${tool.tool_name}-${index}`}>
              <div className="record-header"><strong className="mono">{tool.tool_name}</strong><StatusBadge value={tool.status} /></div>
              <dl className="technical-facts">
                <div><dt>Duration</dt><dd>{duration(tool.duration_ms)}</dd></div>
                <div><dt>Evidence IDs</dt><dd className="mono">{tool.evidence_ids.join(", ") || "None"}</dd></div>
              </dl>
              <p className="technical-label">Arguments</p>
              <pre>{JSON.stringify(tool.tool_arguments, null, 2)}</pre>
              {tool.error ? (
                <dl className="technical-facts technical-error">
                  <div><dt>Error code</dt><dd>{tool.error.code}</dd></div>
                  <div><dt>Error message</dt><dd>{tool.error.message}</dd></div>
                  <div><dt>Retryable</dt><dd>{yesNo(tool.error.retryable)}</dd></div>
                </dl>
              ) : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export function InvestigationTechnicalDetails({
  progress,
  workflow,
}: InvestigationTechnicalDetailsProps) {
  if (!workflow && (!progress || progress.phase === "not_started")) {
    return null;
  }
  const audit = workflow ? auditFacts(workflow) : [];
  const awaitingFirstCheckpoint = progress?.phase === "accepted" && !progress.checkpoint_available;

  return (
    <details className="technical-details">
      <summary>Technical details</summary>
      <p className="technical-description">Workflow stages, Tool calls, counters, and audit identifiers.</p>

      {progress ? (
        <section className="technical-section" aria-labelledby="technical-workflow-execution-heading">
          <h3 id="technical-workflow-execution-heading">Workflow execution</h3>
          {awaitingFirstCheckpoint ? (
            <p>First checkpoint has not been persisted yet.</p>
          ) : (
            <dl className="technical-facts">
              {progress.current_stage ? <div><dt>Latest persisted stage</dt><dd className="mono">{progress.current_stage}</dd></div> : null}
              {progress.pending_tool_name ? <div><dt>Pending Tool</dt><dd className="mono">{progress.pending_tool_name}</dd></div> : null}
              <div><dt>Investigation round</dt><dd>{progress.investigation_round}</dd></div>
              <div><dt>Tool call count</dt><dd>{progress.tool_call_count}</dd></div>
              <div><dt>LLM call count</dt><dd>{progress.llm_call_count}</dd></div>
              <div><dt>Workflow retry count</dt><dd>{progress.workflow_retry_count}</dd></div>
              <div><dt>Checkpoint available</dt><dd>{yesNo(progress.checkpoint_available)}</dd></div>
              <div><dt>Retry available</dt><dd>{yesNo(progress.retry_available)}</dd></div>
              {progress.latest_tool ? <div><dt>Latest Tool</dt><dd><span className="mono">{progress.latest_tool.tool_name}</span> · {progress.latest_tool.status} · {duration(progress.latest_tool.duration_ms)}</dd></div> : null}
              {progress.terminal_reason ? <div><dt>Terminal reason</dt><dd className="mono">{progress.terminal_reason}</dd></div> : null}
              {progress.failure ? (
                <>
                  <div><dt>Failure category</dt><dd className="mono">{progress.failure.category}</dd></div>
                  <div><dt>Failed node</dt><dd className="mono">{progress.failure.failed_node}</dd></div>
                  <div><dt>Safe failure message</dt><dd>{progress.failure.message}</dd></div>
                  <div><dt>Failure retryable</dt><dd>{yesNo(progress.failure.retryable)}</dd></div>
                </>
              ) : null}
            </dl>
          )}
        </section>
      ) : workflow ? (
        <section className="technical-section" aria-labelledby="technical-workflow-execution-heading">
          <h3 id="technical-workflow-execution-heading">Workflow execution</h3>
          <dl className="technical-facts"><div><dt>Latest persisted stage</dt><dd className="mono">{workflow.current_stage}</dd></div></dl>
        </section>
      ) : null}

      {workflow ? <ToolCalls workflow={workflow} /> : null}
      {audit.length > 0 ? (
        <section className="technical-section" aria-labelledby="technical-decision-audit-heading">
          <h3 id="technical-decision-audit-heading">Decision audit</h3>
          <dl className="technical-facts">
            {audit.map((fact) => <div key={fact.label}><dt>{fact.label}</dt><dd className="mono">{fact.value}</dd></div>)}
          </dl>
        </section>
      ) : null}
    </details>
  );
}
