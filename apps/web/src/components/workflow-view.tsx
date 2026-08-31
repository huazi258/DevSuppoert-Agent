import type { WorkflowEvidence, WorkflowHypothesis, WorkflowResponse } from "../lib/types";
import { StatusBadge } from "./status-badge";

interface WorkflowViewProps {
  workflow: WorkflowResponse;
}

interface EvidenceRelationship {
  supporting: number;
  contradicting: number;
}

const EVIDENCE_LABELS: Record<string, string> = {
  search_knowledge: "Knowledge",
  query_logs: "Logs",
  query_metrics: "Metrics",
  query_traces: "Traces",
  get_deployment_history: "Deployment",
};

function confidence(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

function evidenceLabel(evidence: WorkflowEvidence): string {
  return EVIDENCE_LABELS[evidence.source] ?? EVIDENCE_LABELS[evidence.evidence_type] ?? "Runtime Evidence";
}

function referenceSummary(summary: string): string {
  const maximumLength = 220;
  return summary.length <= maximumLength ? summary : `${summary.slice(0, maximumLength - 1).trimEnd()}…`;
}

function buildEvidenceRelationships(hypotheses: WorkflowHypothesis[]): Map<string, EvidenceRelationship> {
  const relationships = new Map<string, EvidenceRelationship>();
  for (const hypothesis of hypotheses) {
    for (const evidenceId of hypothesis.supporting_evidence_ids) {
      const current = relationships.get(evidenceId) ?? { supporting: 0, contradicting: 0 };
      relationships.set(evidenceId, { ...current, supporting: current.supporting + 1 });
    }
    for (const evidenceId of hypothesis.contradicting_evidence_ids) {
      const current = relationships.get(evidenceId) ?? { supporting: 0, contradicting: 0 };
      relationships.set(evidenceId, { ...current, contradicting: current.contradicting + 1 });
    }
  }
  return relationships;
}

function EvidenceReferenceList({
  evidenceById,
  evidenceIds,
  relationship,
}: {
  evidenceById: Map<string, WorkflowEvidence>;
  evidenceIds: string[];
  relationship: "Supporting" | "Contradicting";
}) {
  const heading = `${relationship} evidence`;
  if (evidenceIds.length === 0) {
    return <section className="evidence-reference-list"><h4>{heading}</h4><p className="empty-state">None recorded.</p></section>;
  }

  return (
    <section className={`evidence-reference-list ${relationship.toLowerCase()}`}>
      <h4>{heading}</h4>
      <ul>
        {evidenceIds.map((evidenceId) => {
          const evidence = evidenceById.get(evidenceId);
          return (
            <li key={evidenceId}>
              {evidence ? (
                <a href={`#evidence-${evidence.id}`}>
                  <strong>{evidenceLabel(evidence)}:</strong> {referenceSummary(evidence.summary)}
                </a>
              ) : (
                <span className="evidence-reference-unavailable">Referenced evidence unavailable.</span>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function HypothesisCard({
  evidenceById,
  hypothesis,
}: {
  evidenceById: Map<string, WorkflowEvidence>;
  hypothesis: WorkflowHypothesis;
}) {
  return (
    <article className="record-card hypothesis-card">
      <div className="record-header"><h3>{hypothesis.summary}</h3><StatusBadge value={hypothesis.status} /></div>
      <dl className="detail-grid">
        <div><dt>Confidence</dt><dd>{confidence(hypothesis.confidence)}</dd></div>
        {hypothesis.next_check ? <div><dt>Next check</dt><dd>{hypothesis.next_check}</dd></div> : null}
      </dl>
      <EvidenceReferenceList
        evidenceById={evidenceById}
        evidenceIds={hypothesis.supporting_evidence_ids}
        relationship="Supporting"
      />
      <EvidenceReferenceList
        evidenceById={evidenceById}
        evidenceIds={hypothesis.contradicting_evidence_ids}
        relationship="Contradicting"
      />
    </article>
  );
}

function EvidenceCard({
  evidence,
  relationship,
}: {
  evidence: WorkflowEvidence;
  relationship: EvidenceRelationship | undefined;
}) {
  const label = evidenceLabel(evidence);
  const relationshipText = [
    relationship?.supporting ? `Supports ${relationship.supporting} hypothesis${relationship.supporting === 1 ? "" : "es"}` : null,
    relationship?.contradicting ? `Contradicts ${relationship.contradicting} hypothesis${relationship.contradicting === 1 ? "" : "es"}` : null,
  ].filter(Boolean);

  return (
    <article className="record-card evidence-card" id={`evidence-${evidence.id}`}>
      <h4>{label}</h4>
      <p>{evidence.summary}</p>
      {evidence.citation ? (
        <section className="citation-block" aria-label="Knowledge source">
          <h5>Source</h5>
          <dl>
            <div><dt>Document reference</dt><dd>{evidence.citation.document_reference}</dd></div>
            <div><dt>Section</dt><dd>{evidence.citation.section}</dd></div>
            <div><dt>Source</dt><dd>{evidence.citation.source}</dd></div>
          </dl>
        </section>
      ) : label === "Knowledge" && evidence.reference ? (
        <p className="evidence-provenance"><strong>Reference:</strong> {evidence.reference}</p>
      ) : null}
      {relationshipText.length > 0 ? <p className="evidence-relationships">Referenced by: {relationshipText.join(" · ")}</p> : null}
    </article>
  );
}

function EvidenceGroup({
  evidence,
  relationships,
  title,
}: {
  evidence: WorkflowEvidence[];
  relationships: Map<string, EvidenceRelationship>;
  title: "Knowledge" | "Runtime Evidence";
}) {
  if (evidence.length === 0) {
    return null;
  }
  const headingId = `${title.toLowerCase().replaceAll(" ", "-")}-evidence-heading`;
  return (
    <section className="evidence-group" aria-labelledby={headingId}>
      <h3 id={headingId}>{title}</h3>
      <div className="stack-list">
        {evidence.map((item) => <EvidenceCard evidence={item} key={item.id} relationship={relationships.get(item.id)} />)}
      </div>
    </section>
  );
}

function IdList({ ids }: { ids: string[] }) {
  return ids.length > 0 ? <span className="mono">{ids.join(", ")}</span> : <span>—</span>;
}

export function WorkflowView({ workflow }: WorkflowViewProps) {
  const evidenceById = new Map(workflow.evidence.map((evidence) => [evidence.id, evidence]));
  const relationships = buildEvidenceRelationships(workflow.hypotheses);
  const knowledgeEvidence = workflow.evidence.filter((evidence) => evidenceLabel(evidence) === "Knowledge");
  const runtimeEvidence = workflow.evidence.filter((evidence) => evidenceLabel(evidence) !== "Knowledge");

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
            {workflow.hypotheses.map((hypothesis) => <HypothesisCard evidenceById={evidenceById} hypothesis={hypothesis} key={hypothesis.id} />)}
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-heading"><h2>Evidence</h2><span>{workflow.evidence.length}</span></div>
        {workflow.evidence.length === 0 ? <p className="empty-state">No evidence recorded yet.</p> : (
          <div className="evidence-groups">
            <EvidenceGroup evidence={knowledgeEvidence} relationships={relationships} title="Knowledge" />
            <EvidenceGroup evidence={runtimeEvidence} relationships={relationships} title="Runtime Evidence" />
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
