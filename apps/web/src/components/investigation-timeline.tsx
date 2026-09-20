import { formatDate, type InvestigationTimelineEvent, type WorkflowTimelineResponse } from "../lib/types";

interface InvestigationTimelineProps {
  timeline: WorkflowTimelineResponse;
}

const eventLabels: Record<InvestigationTimelineEvent["event_type"], string> = {
  investigation_started: "调查已启动",
  knowledge_searched: "已检索团队知识",
  hypothesis_created: "已形成待验证假设",
  evidence_collected: "已收集运行证据",
  hypothesis_updated: "假设已更新",
  conclusion_reached: "已形成调查结论",
  investigation_interrupted: "调查被中断",
  investigation_completed: "调查已结束",
};

const visibleEventTypes = new Set(Object.keys(eventLabels));

function eventSummary(event: InvestigationTimelineEvent): string {
  if (event.event_type === "investigation_started") {
    return "调查状态已持久化，开始按范围收集证据。";
  }
  if (event.event_type === "knowledge_searched") {
    return "已检索与当前调查目标和服务范围匹配的团队知识。";
  }
  if (event.event_type === "evidence_collected") {
    return "已收集新的运行证据，正在用于验证假设。";
  }
  if (event.event_type === "investigation_completed") {
    return "当前调查轮次已结束，结果已持久化。";
  }
  return event.summary;
}

export function InvestigationTimeline({ timeline }: InvestigationTimelineProps) {
  const visibleEvents = timeline.events.filter((event) => visibleEventTypes.has(event.event_type));
  return (
    <section className="panel" aria-labelledby="investigation-timeline-heading">
      <p className="eyebrow">调查过程</p>
      <h2 id="investigation-timeline-heading">调查时间线</h2>
      {timeline.truncated ? <p className="subtle-status">较早的调查事件未在当前视图中展示。</p> : null}
      {visibleEvents.length === 0 ? <p className="empty-state">尚未记录可展示的调查事件。</p> : (
        <ol className="timeline investigation-timeline">
          {visibleEvents.map((event, index) => (
            <li className={index === visibleEvents.length - 1 ? "timeline-latest" : undefined} key={event.event_id}>
              <strong>{eventLabels[event.event_type]}</strong>
              <p>{eventSummary(event)}</p>
              <small className="subtle-status">
                {event.occurred_at ? formatDate(event.occurred_at) : "等待首个已持久化进度"}
              </small>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
