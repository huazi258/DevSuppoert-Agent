import type { WorkflowResponse } from "../lib/types";
import { StatusBadge } from "./status-badge";

interface WorkflowViewProps {
  workflow: WorkflowResponse;
}

function confidence(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

function IdList({ ids }: { ids: string[] }) {
  return ids.length > 0 ? <span className="mono">{ids.join(", ")}</span> : <span>—</span>;
}

export function WorkflowView({ workflow }: WorkflowViewProps) {
  return (
    <section className="workflow-view" aria-label="Investigation details">
      {workflow.current_goal ? (
        <section className="panel">
          <p className="eyebrow">Current goal</p>
          <p>{workflow.current_goal}</p>
        </section>
      ) : null}

      <section className="panel">
        <div className="panel-heading"><h2>Hypotheses</h2><span>{workflow.hypotheses.length}</span></div>
        {workflow.hypotheses.length === 0 ? <p className="empty-state">No hypotheses recorded yet.</p> : (
          <div className="stack-list">
            {workflow.hypotheses.map((hypothesis) => (
              <article className="record-card" key={hypothesis.id}>
                <div className="record-header"><strong>{hypothesis.summary}</strong><StatusBadge value={hypothesis.status} /></div>
                <dl className="detail-grid">
                  <div><dt>Confidence</dt><dd>{confidence(hypothesis.confidence)}</dd></div>
                  <div><dt>Next check</dt><dd>{hypothesis.next_check ?? "—"}</dd></div>
                  <div className="full-detail"><dt>Supporting Evidence</dt><dd><IdList ids={hypothesis.supporting_evidence_ids} /></dd></div>
                  <div className="full-detail"><dt>Contradicting Evidence</dt><dd><IdList ids={hypothesis.contradicting_evidence_ids} /></dd></div>
                </dl>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-heading"><h2>Evidence</h2><span>{workflow.evidence.length}</span></div>
        {workflow.evidence.length === 0 ? <p className="empty-state">No Evidence recorded yet.</p> : (
          <div className="stack-list">
            {workflow.evidence.map((evidence) => (
              <article className="record-card" key={evidence.id}>
                <p className="mono compact-id">{evidence.id}</p>
                <strong>{evidence.evidence_type}</strong>
                <dl className="detail-grid">
                  <div><dt>Source</dt><dd>{evidence.source}</dd></div>
                  <div><dt>Reference</dt><dd>{evidence.reference ?? "—"}</dd></div>
                  <div className="full-detail"><dt>Summary</dt><dd>{evidence.summary}</dd></div>
                </dl>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-heading"><h2>Tool Timeline</h2><span>{workflow.tool_history.length}</span></div>
        {workflow.tool_history.length === 0 ? <p className="empty-state">No Tool calls recorded yet.</p> : (
          <ol className="timeline">
            {workflow.tool_history.map((tool, index) => (
              <li key={`${tool.tool_name}-${index}`}>
                <div className="record-header"><strong>{tool.tool_name}</strong><StatusBadge value={tool.status} /></div>
                <p>Duration: {tool.duration_ms === null ? "—" : `${tool.duration_ms.toFixed(0)} ms`}</p>
                <p>Evidence: <IdList ids={tool.evidence_ids} /></p>
                <pre>{JSON.stringify(tool.tool_arguments, null, 2)}</pre>
                {tool.error ? <p className="error-banner">{tool.error.code}: {tool.error.message} {tool.error.retryable ? "(retryable)" : ""}</p> : null}
              </li>
            ))}
          </ol>
        )}
      </section>

      {workflow.final_conclusion ? (
        <section className="panel">
          <p className="eyebrow">Final conclusion</p>
          <h2>{workflow.final_conclusion.summary}</h2>
          <dl className="detail-grid">
            <div><dt>Root cause</dt><dd>{workflow.final_conclusion.root_cause ?? "—"}</dd></div>
            <div><dt>Confidence</dt><dd>{confidence(workflow.final_conclusion.confidence)}</dd></div>
            <div className="full-detail"><dt>Recommended next action</dt><dd>{workflow.final_conclusion.recommended_next_action ?? "—"}</dd></div>
          </dl>
        </section>
      ) : null}
    </section>
  );
}
