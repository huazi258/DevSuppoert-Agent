import type { WorkflowEvidence, WorkflowHypothesis, WorkflowResponse } from "../lib/types";

interface WorkflowViewProps {
  workflow: WorkflowResponse;
}

interface EvidenceRelationship {
  supporting: number;
  contradicting: number;
}

const evidenceLabels: Record<string, string> = {
  search_knowledge: "知识库证据",
  query_logs: "日志证据",
  query_metrics: "指标证据",
  query_traces: "链路证据",
  get_deployment_history: "部署事实",
};

const evidenceTypeLabels: Record<string, string> = {
  knowledge_retrieval: "知识检索",
  log_event: "日志事件",
  metric_snapshot: "指标快照",
  trace: "链路追踪",
  deployment_fact: "部署事实",
};

function evidenceLabel(evidence: WorkflowEvidence): string {
  return evidenceLabels[evidence.source] ?? evidenceTypeLabels[evidence.evidence_type] ?? "运行证据";
}

function buildEvidenceRelationships(
  hypotheses: WorkflowHypothesis[],
  workflow: WorkflowResponse,
): Map<string, EvidenceRelationship> {
  const relationships = new Map<string, EvidenceRelationship>();
  const add = (evidenceIds: string[], relationship: keyof EvidenceRelationship) => {
    for (const evidenceId of evidenceIds) {
      const current = relationships.get(evidenceId) ?? { supporting: 0, contradicting: 0 };
      relationships.set(evidenceId, { ...current, [relationship]: current[relationship] + 1 });
    }
  };
  for (const hypothesis of hypotheses) {
    add(hypothesis.supporting_evidence_ids, "supporting");
    add(hypothesis.contradicting_evidence_ids, "contradicting");
  }
  if (workflow.final_conclusion) {
    add(workflow.final_conclusion.supporting_evidence_ids, "supporting");
    add(workflow.final_conclusion.contradicting_evidence_ids, "contradicting");
  }
  return relationships;
}

function relationshipText(relationship: EvidenceRelationship | undefined): string | null {
  if (!relationship) {
    return null;
  }
  const items = [
    relationship.supporting > 0 ? `支持 ${relationship.supporting} 项假设或结论` : null,
    relationship.contradicting > 0 ? `反驳 ${relationship.contradicting} 项假设或结论` : null,
  ].filter((item): item is string => item !== null);
  return items.length > 0 ? items.join("；") : null;
}

function EvidenceCard({
  evidence,
  relationship,
}: {
  evidence: WorkflowEvidence;
  relationship: EvidenceRelationship | undefined;
}) {
  const relationshipSummary = relationshipText(relationship);
  return (
    <article className="record-card evidence-card" id={`evidence-${evidence.id}`}>
      <div className="record-header"><h3>{evidenceLabel(evidence)}</h3><span className="subtle-status">来源 Tool：{evidence.source}</span></div>
      <p>{evidence.summary}</p>
      {evidence.citation ? (
        <section className="citation-block" aria-label="知识引用">
          <h4>知识引用</h4>
          <dl>
            <div><dt>文档</dt><dd>{evidence.citation.document_title}</dd></div>
            <div><dt>章节</dt><dd>{evidence.citation.section}</dd></div>
            <div><dt>引用标识</dt><dd>{evidence.citation.document_reference}</dd></div>
          </dl>
        </section>
      ) : evidence.reference ? (
        <p className="evidence-provenance">引用：{evidence.reference}</p>
      ) : null}
      {relationshipSummary ? <p className="evidence-relationships">证据关系：{relationshipSummary}</p> : null}
    </article>
  );
}

export function WorkflowView({ workflow }: WorkflowViewProps) {
  const relationships = buildEvidenceRelationships(workflow.hypotheses, workflow);
  return (
    <section className="panel" aria-labelledby="key-evidence-heading">
      <div className="panel-heading">
        <div><p className="eyebrow">关键证据</p><h2 id="key-evidence-heading">证据与引用</h2></div>
        <span>{workflow.evidence.length}</span>
      </div>
      {workflow.evidence.length === 0 ? <p className="empty-state">尚未记录可展示的证据。</p> : (
        <div className="stack-list">
          {workflow.evidence.map((evidence) => (
            <EvidenceCard evidence={evidence} key={evidence.id} relationship={relationships.get(evidence.id)} />
          ))}
        </div>
      )}
    </section>
  );
}
