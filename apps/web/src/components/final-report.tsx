import type { FinalReport, FinalReportEvidence } from "../lib/types";
import { formatDate } from "../lib/types";
import { StatusBadge } from "./status-badge";

interface FinalReportViewProps {
  report: FinalReport;
}

function confidence(value: number | null): string {
  return value === null ? "暂未评估" : `${Math.round(value * 100)}%`;
}

function ReportEvidence({ evidence }: { evidence: FinalReportEvidence }) {
  return (
    <li>
      <strong>{evidence.source}</strong>：{evidence.summary}
      {evidence.citation ? (
        <span>（{evidence.citation.document_title} / {evidence.citation.section} / {evidence.citation.document_reference}）</span>
      ) : evidence.reference ? <span>（{evidence.reference}）</span> : null}
    </li>
  );
}

export function FinalReportView({ report }: FinalReportViewProps) {
  const content = report.content;
  const confirmedRootCause =
    content.final_status === "CONCLUDED" ? content.conclusion?.root_cause ?? null : null;
  return (
    <section className="panel" aria-labelledby="final-report-heading">
      <div className="panel-heading">
        <div><p className="eyebrow">已持久化报告</p><h2 id="final-report-heading">本轮调查报告</h2></div>
        <StatusBadge value={content.final_status} />
      </div>
      <section className="report-section">
        <h3>调查结论</h3>
        {content.conclusion ? (
          <dl className="detail-grid">
            <div className="full-detail"><dt>结论摘要</dt><dd>{content.conclusion.summary}</dd></div>
            <div><dt>置信度</dt><dd>{confidence(content.conclusion.confidence)}</dd></div>
            {confirmedRootCause ? <div className="full-detail"><dt>根因判断</dt><dd>{confirmedRootCause}</dd></div> : null}
          </dl>
        ) : <p className="empty-state">本轮调查未形成可确认的根因结论。</p>}
      </section>
      <section className="report-section">
        <h3>未确认事项</h3>
        {content.unknowns.length > 0 ? <ul className="simple-list">{content.unknowns.map((item) => <li key={item}>{item}</li>)}</ul> : <p className="empty-state">无。</p>}
      </section>
      <section className="report-section">
        <h3>关键证据与引用</h3>
        {content.key_evidence.length > 0 ? <ul className="simple-list">{content.key_evidence.map((item) => <ReportEvidence evidence={item} key={item.id} />)}</ul> : <p className="empty-state">本轮没有可引用的关键证据。</p>}
      </section>
      <section className="report-section">
        <h3>人工下一步建议</h3>
        {content.manual_suggestions.length > 0 ? <ul className="simple-list">{content.manual_suggestions.map((item) => <li key={item}>{item}</li>)}</ul> : <p className="empty-state">暂无额外建议。</p>}
      </section>
      <section className="report-section">
        <h3>调查输入</h3>
        <dl className="detail-grid">
          <div><dt>服务</dt><dd>{content.input_summary.service}</dd></div>
          <div><dt>环境</dt><dd>{content.input_summary.environment}</dd></div>
          <div><dt>开始时间</dt><dd>{formatDate(content.input_summary.time_range_start)}</dd></div>
          <div><dt>结束时间</dt><dd>{formatDate(content.input_summary.time_range_end)}</dd></div>
          <div className="full-detail"><dt>观察到的现象</dt><dd>{content.input_summary.description}</dd></div>
        </dl>
      </section>
      <section className="report-section">
        <h3>终态原因</h3>
        <p className="mono">{content.terminal_reason ?? "未记录"}</p>
      </section>
    </section>
  );
}
