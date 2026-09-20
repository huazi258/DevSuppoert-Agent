import { formatDate, type InvestigationRound } from "../lib/types";
import { StatusBadge } from "./status-badge";

interface RoundHistoryProps {
  rounds: InvestigationRound[];
  loading: boolean;
}

export function RoundHistory({ rounds, loading }: RoundHistoryProps) {
  return (
    <section className="panel" aria-labelledby="round-history-heading">
      <div className="panel-heading">
        <div><p className="eyebrow">历史调查轮次</p><h2 id="round-history-heading">调查轮次与报告</h2></div>
        <span>{rounds.length}</span>
      </div>
      {loading && rounds.length === 0 ? <p className="empty-state">正在加载调查轮次…</p> : null}
      {!loading && rounds.length === 0 ? <p className="empty-state">尚未找到调查轮次记录。</p> : null}
      <div className="stack-list">
        {rounds.map((round) => (
          <article className={`record-card ${round.is_current ? "round-card-current" : ""}`} key={round.round_id}>
            <div className="record-header">
              <h3>第 {round.round_number} 轮{round.is_current ? "（当前）" : ""}</h3>
              <StatusBadge value={round.status} />
            </div>
            <dl className="detail-grid">
              <div><dt>开始时间</dt><dd>{formatDate(round.started_at)}</dd></div>
              <div><dt>结束时间</dt><dd>{round.completed_at ? formatDate(round.completed_at) : "进行中"}</dd></div>
              <div className="full-detail"><dt>待验证观察</dt><dd>{round.triggering_observation?.content ?? "初始调查"}</dd></div>
              {round.terminal_reason ? <div className="full-detail"><dt>终态原因</dt><dd className="mono">{round.terminal_reason}</dd></div> : null}
              <div><dt>报告</dt><dd>{round.report ? "已生成" : "尚未生成"}</dd></div>
              {round.status === "CONCLUDED" && round.report?.conclusion_summary ? <div className="full-detail"><dt>结论摘要</dt><dd>{round.report.conclusion_summary}</dd></div> : null}
            </dl>
          </article>
        ))}
      </div>
    </section>
  );
}
