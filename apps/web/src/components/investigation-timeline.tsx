import { formatDate, type WorkflowTimelineResponse } from "../lib/types";
import { StatusBadge } from "./status-badge";

interface InvestigationTimelineProps {
  timeline: WorkflowTimelineResponse;
}

export function InvestigationTimeline({ timeline }: InvestigationTimelineProps) {
  return (
    <section className="panel" aria-labelledby="investigation-timeline-heading">
      <p className="eyebrow">Investigation</p>
      <h2 id="investigation-timeline-heading">Investigation Timeline</h2>
      {timeline.truncated ? <p className="subtle-status">Earlier investigation events are omitted.</p> : null}
      {timeline.events.length === 0 ? <p className="empty-state">No investigation events recorded yet.</p> : (
        <ol className="timeline investigation-timeline">
          {timeline.events.map((event, index) => (
            <li className={index === timeline.events.length - 1 ? "timeline-latest" : undefined} key={event.event_id}>
              <div className="record-header">
                <strong>{event.title}</strong>
                {event.status ? <StatusBadge value={event.status} /> : null}
              </div>
              <p>{event.summary}</p>
              <small className="subtle-status">
                {event.occurred_at ? formatDate(event.occurred_at) : "Pending first checkpoint"}
              </small>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
