import type { ReactNode } from "react";

import type { FinalReport } from "../lib/types";
import { formatDate } from "../lib/types";
import { StatusBadge } from "./status-badge";

interface FinalReportViewProps {
  report: FinalReport;
}

function optionalSection(title: string, content: ReactNode) {
  return <section className="report-section"><h3>{title}</h3>{content}</section>;
}

export function FinalReportView({ report }: FinalReportViewProps) {
  const content = report.content;
  return (
    <section className="panel" aria-labelledby="final-report-heading">
      <div className="panel-heading">
        <div><p className="eyebrow">Persisted final report</p><h2 id="final-report-heading">Final Report</h2></div>
        <StatusBadge value={content.final_status} />
      </div>
      {optionalSection("Incident Summary", (
        <dl className="detail-grid">
          <div><dt>Service</dt><dd>{content.incident_summary.service}</dd></div>
          <div><dt>Environment</dt><dd>{content.incident_summary.environment}</dd></div>
          <div><dt>Window start</dt><dd>{formatDate(content.incident_summary.time_range_start)}</dd></div>
          <div><dt>Window end</dt><dd>{formatDate(content.incident_summary.time_range_end)}</dd></div>
          <div className="full-detail"><dt>Description</dt><dd>{content.incident_summary.description}</dd></div>
        </dl>
      ))}
      {optionalSection("Root Cause", content.root_cause ? (
        <div><p>{content.root_cause.summary}</p><p>{content.root_cause.root_cause ?? "No confirmed root cause recorded."}</p></div>
      ) : <p className="empty-state">No confirmed root cause recorded.</p>)}
      {optionalSection("Timeline", (
        <ol className="timeline">
          {content.timeline.map((item) => <li key={item.record_id}><strong>{item.type}</strong><p>{item.summary}</p><small>{formatDate(item.timestamp)}</small></li>)}
        </ol>
      ))}
      {optionalSection("Hypotheses", content.hypotheses.length > 0 ? (
        <ul className="simple-list">{content.hypotheses.map((item) => <li key={item.id}>{item.summary} — {item.status}</li>)}</ul>
      ) : <p className="empty-state">No hypotheses recorded.</p>)}
      {optionalSection("Key Evidence", content.key_evidence.length > 0 ? (
        <ul className="simple-list">{content.key_evidence.map((item) => <li key={item.id}>{item.evidence_type}: {item.summary}</li>)}</ul>
      ) : <p className="empty-state">No key Evidence recorded.</p>)}
      {optionalSection("Recommended Action", content.recommended_action ? <div><p>{content.recommended_action.summary}</p><p>{content.recommended_action.reason}</p><p>Risk: {content.recommended_action.risk}</p></div> : <p className="empty-state">No recommended action recorded.</p>)}
      {optionalSection("Action", content.action ? <div><p>{content.action.action_type} — {content.action.status}</p><pre>{JSON.stringify(content.action.parameters, null, 2)}</pre></div> : <p className="empty-state">No Action recorded.</p>)}
      {optionalSection("Approval", content.approval ? <p>{content.approval.status} · {formatDate(content.approval.updated_at)}</p> : <p className="empty-state">No Approval recorded.</p>)}
      {optionalSection("Execution", content.execution ? <p>{content.execution.status} · executed: {String(content.execution.executed)}</p> : <p className="empty-state">No Execution recorded.</p>)}
      {optionalSection("Verification", content.verification ? <div><p>{content.verification.status} — {content.verification.summary}</p><pre>{JSON.stringify(content.verification.details, null, 2)}</pre></div> : <p className="empty-state">No Verification recorded.</p>)}
    </section>
  );
}
