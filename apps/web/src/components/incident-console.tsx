"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  continueInvestigation,
  getFinalReport,
  getIncident,
  getWorkflow,
  getWorkflowProgress,
  getWorkflowTimeline,
  listInvestigationRounds,
  retryWorkflow,
  startWorkflow,
} from "../lib/api";
import {
  formatDate,
  type FinalReport,
  type Incident,
  type InvestigationRound,
  type SupplementalObservationInput,
  type WorkflowProgressResponse,
  type WorkflowResponse,
  type WorkflowTimelineResponse,
} from "../lib/types";
import { FinalReportView } from "./final-report";
import { ContinuationForm } from "./continuation-form";
import { InvestigationTechnicalDetails } from "./investigation-technical-details";
import { InvestigationTimeline } from "./investigation-timeline";
import { StatusBadge } from "./status-badge";
import { RoundHistory } from "./round-history";
import { WorkflowView } from "./workflow-view";

interface IncidentConsoleProps {
  incidentId: string;
}

const terminalStatuses = new Set(["CONCLUDED", "INCONCLUSIVE", "FAILED"]);

function confidence(value: number | null): string {
  return value === null ? "暂未评估" : `${Math.round(value * 100)}%`;
}

function isWorkflowNotStarted(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404 && error.detail === "Workflow not started";
}

function messageFor(error: unknown, fallback: string): string {
  return error instanceof ApiError && error.status === 0 ? error.detail : fallback;
}

function progressSummary(progress: WorkflowProgressResponse | null): string | null {
  if (!progress) {
    return null;
  }
  if (progress.phase === "accepted") {
    return "调查已受理，正在等待首个已持久化进度。";
  }
  if (progress.phase === "running") {
    return progress.current_goal ?? "正在收集证据并验证假设。";
  }
  if (progress.phase === "failed") {
    return progress.failure?.message ?? "调查运行遇到受控错误。";
  }
  return progress.current_goal;
}

function suggestions(
  workflow: WorkflowResponse | null,
  report: FinalReport | null,
): string[] {
  const reportSuggestions = report?.content.manual_suggestions ?? [];
  if (reportSuggestions.length > 0) {
    return reportSuggestions;
  }
  const recommended = workflow?.final_conclusion?.recommended_next_action;
  return recommended ? [recommended] : [];
}

export function IncidentConsole({ incidentId }: IncidentConsoleProps) {
  const [incident, setIncident] = useState<Incident | null>(null);
  const [workflow, setWorkflow] = useState<WorkflowResponse | null>(null);
  const [progress, setProgress] = useState<WorkflowProgressResponse | null>(null);
  const [timeline, setTimeline] = useState<WorkflowTimelineResponse | null>(null);
  const [report, setReport] = useState<FinalReport | null>(null);
  const [rounds, setRounds] = useState<InvestigationRound[]>([]);
  const [loading, setLoading] = useState(true);
  const [workflowLoading, setWorkflowLoading] = useState(false);
  const [mutationPending, setMutationPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [workflowError, setWorkflowError] = useState<string | null>(null);
  const [progressError, setProgressError] = useState<string | null>(null);
  const [timelineError, setTimelineError] = useState<string | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [roundsError, setRoundsError] = useState<string | null>(null);
  const [roundsLoading, setRoundsLoading] = useState(true);
  const refreshInFlight = useRef(false);
  const reportRequested = useRef(false);

  const refresh = useCallback(async () => {
    setWorkflowLoading(true);
    try {
      const nextIncident = await getIncident(incidentId);
      setIncident(nextIncident);
      setError(null);
      setRoundsLoading(true);
      try {
        setRounds(await listInvestigationRounds(incidentId));
        setRoundsError(null);
      } catch (roundsLoadError: unknown) {
        setRounds([]);
        setRoundsError(messageFor(roundsLoadError, "历史调查轮次加载失败，请稍后重试。"));
      } finally {
        setRoundsLoading(false);
      }
      try {
        const nextProgress = await getWorkflowProgress(incidentId);
        setProgress(nextProgress);
        setProgressError(null);
      } catch (progressLoadError: unknown) {
        setProgress(null);
        setProgressError(messageFor(progressLoadError, "调查进度加载失败，请稍后重试。"));
      }
      try {
        const nextTimeline = await getWorkflowTimeline(incidentId);
        setTimeline(nextTimeline);
        setTimelineError(null);
      } catch (timelineLoadError: unknown) {
        setTimeline(null);
        setTimelineError(messageFor(timelineLoadError, "调查过程加载失败，请稍后重试。"));
      }
      try {
        const nextWorkflow = await getWorkflow(incidentId);
        setWorkflow(nextWorkflow);
        setWorkflowError(null);
      } catch (workflowLoadError: unknown) {
        if (isWorkflowNotStarted(workflowLoadError)) {
          setWorkflow(null);
          setWorkflowError(null);
        } else {
          setWorkflow(null);
          setWorkflowError(messageFor(workflowLoadError, "调查详情加载失败，请稍后重试。"));
        }
      }
    } catch (incidentLoadError: unknown) {
      setIncident(null);
      setWorkflow(null);
      setProgress(null);
      setTimeline(null);
      setReport(null);
      setRounds([]);
      setWorkflowError(null);
      setProgressError(null);
      setTimelineError(null);
      setReportError(null);
      setRoundsError(null);
      setRoundsLoading(false);
      setError(messageFor(incidentLoadError, "故障调查加载失败，请稍后重试。"));
    } finally {
      setLoading(false);
      setWorkflowLoading(false);
    }
  }, [incidentId]);

  useEffect(() => {
    reportRequested.current = false;
    setIncident(null);
    setWorkflow(null);
    setProgress(null);
    setTimeline(null);
    setReport(null);
    setRounds([]);
    setError(null);
    setMutationError(null);
    setWorkflowError(null);
    setProgressError(null);
    setTimelineError(null);
    setReportError(null);
    setRoundsError(null);
    setRoundsLoading(true);
    setLoading(true);
    void refresh();
  }, [incidentId, refresh]);

  useEffect(() => {
    if (!incident || incident.status !== "INVESTIGATING" || mutationPending) {
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
  }, [incident, mutationPending, refresh]);

  const shouldFetchReport = Boolean(
    incident && (terminalStatuses.has(incident.status) || workflow?.report_outcome),
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
            ? "调查报告尚未生成。"
            : messageFor(reportLoadError, "调查报告加载失败，请稍后重试。"),
        );
      });
  }, [incidentId, shouldFetchReport]);

  async function startInvestigation() {
    setMutationPending(true);
    setMutationError(null);
    try {
      await startWorkflow(incidentId);
      await refresh();
    } catch (startError: unknown) {
      setMutationError(messageFor(startError, "开始调查失败，请稍后重试。"));
      await refresh();
    } finally {
      setMutationPending(false);
    }
  }

  async function retryInvestigation() {
    setMutationPending(true);
    setMutationError(null);
    try {
      await retryWorkflow(incidentId);
      await refresh();
    } catch (retryError: unknown) {
      setMutationError(messageFor(retryError, "重试调查失败，请稍后重试。"));
      await refresh();
    } finally {
      setMutationPending(false);
    }
  }

  async function continueWithObservation(input: SupplementalObservationInput): Promise<boolean> {
    setMutationPending(true);
    setMutationError(null);
    try {
      await continueInvestigation(incidentId, input);
      reportRequested.current = false;
      setWorkflow(null);
      setProgress(null);
      setTimeline(null);
      setReport(null);
      setRounds([]);
      setWorkflowError(null);
      setProgressError(null);
      setTimelineError(null);
      setReportError(null);
      setRoundsError(null);
      await refresh();
      return true;
    } catch (continuationError: unknown) {
      setMutationError(messageFor(continuationError, "创建新的调查轮次失败，请稍后重试。"));
      return false;
    } finally {
      setMutationPending(false);
    }
  }

  if (loading && incident === null) {
    return <main className="page-shell console-shell"><p className="empty-state">正在加载故障调查…</p></main>;
  }

  if (incident === null) {
    return (
      <main className="page-shell console-shell">
        <Link className="back-link" href="/">← 返回故障调查列表</Link>
        <p className="error-banner" role="alert">{error ?? "故障调查不存在或暂不可用。"}</p>
      </main>
    );
  }

  // A failed optional progress read must not hide the only way to begin an OPEN
  // V2 investigation.  The backend still atomically validates this transition.
  const canStart = incident.status === "OPEN" && workflow === null && !mutationPending;
  const canRetry =
    incident.status === "FAILED" &&
    Boolean(workflow?.retry_available || progress?.retry_available) &&
    !workflowLoading;
  const currentConclusion = workflow?.final_conclusion ?? report?.content.conclusion ?? null;
  const currentRootCause =
    incident.status === "CONCLUDED" ? currentConclusion?.root_cause ?? null : null;
  const unresolved = report?.content.unknowns ?? workflow?.hypotheses
    .filter((hypothesis) => hypothesis.status !== "CONFIRMED")
    .map((hypothesis) => hypothesis.summary) ?? [];
  const nextSteps = suggestions(workflow, report);

  return (
    <main className="page-shell console-shell">
      <Link className="back-link" href="/">← 返回故障调查列表</Link>
      <header className="console-header">
        <div>
          <p className="eyebrow">故障调查概览</p>
          <h1>{incident.service}</h1>
          <p>{incident.description}</p>
          <p className="mono compact-id">{incident.id}</p>
        </div>
        <dl className="header-facts">
          <div><dt>环境</dt><dd>{incident.environment}</dd></div>
          <div><dt>发生时间</dt><dd>{formatDate(incident.time_range_start)} 至 {formatDate(incident.time_range_end)}</dd></div>
          <div><dt>调查状态</dt><dd><StatusBadge value={incident.status} /></dd></div>
        </dl>
      </header>

      {error ? <p className="error-banner" role="alert">{error}</p> : null}
      {mutationError ? <p className="error-banner" role="alert">{mutationError}</p> : null}
      {workflowError ? <p className="error-banner" role="alert">{workflowError}</p> : null}
      {progressError ? <p className="error-banner" role="alert">{progressError}</p> : null}
      {timelineError ? <p className="error-banner" role="alert">{timelineError}</p> : null}
      {reportError ? <p className="empty-state">{reportError}</p> : null}
      {roundsError ? <p className="empty-state">{roundsError}</p> : null}

      {canStart ? (
        <section className="panel start-panel">
          <div><p className="eyebrow">准备就绪</p><h2>尚未开始调查</h2><p>开始后，系统会以只读方式收集证据并验证假设。</p></div>
          <button className="button primary-button" disabled={mutationPending} onClick={() => void startInvestigation()} type="button">
            {mutationPending ? "正在开始…" : "开始调查"}
          </button>
        </section>
      ) : null}

      <section className="panel" aria-labelledby="current-investigation-heading">
        <p className="eyebrow">当前调查状态</p>
        <h2 id="current-investigation-heading">{currentConclusion?.summary ?? progressSummary(progress) ?? "等待开始调查"}</h2>
        <dl className="detail-grid">
          <div><dt>状态</dt><dd><StatusBadge value={incident.status} /></dd></div>
          {currentConclusion ? <div><dt>置信度</dt><dd>{confidence(currentConclusion.confidence)}</dd></div> : null}
          {currentRootCause ? <div className="full-detail"><dt>当前根因判断</dt><dd>{currentRootCause}</dd></div> : null}
          {unresolved.length > 0 ? <div className="full-detail"><dt>未确认事项</dt><dd>{unresolved.join("；")}</dd></div> : null}
          {progress?.failure ? <div className="full-detail"><dt>受控失败说明</dt><dd>{progress.failure.message}</dd></div> : null}
        </dl>
        {canRetry ? (
          <div className="retry-controls">
            <p>运行时允许继续该调查，可在不改变已持久化证据的前提下重试。</p>
            <button className="button secondary-button" disabled={mutationPending} onClick={() => void retryInvestigation()} type="button">
              {mutationPending ? "正在重试…" : "重试调查"}
            </button>
          </div>
        ) : null}
      </section>

      <section className="panel" aria-labelledby="next-steps-heading">
        <p className="eyebrow">建议下一步</p>
        <h2 id="next-steps-heading">供人工参考</h2>
        {nextSteps.length > 0 ? (
          <ul className="simple-list">{nextSteps.map((step) => <li key={step}>{step}</li>)}</ul>
        ) : <p className="empty-state">调查尚未形成可供人工执行的建议。</p>}
      </section>

      {terminalStatuses.has(incident.status) ? <ContinuationForm onContinue={continueWithObservation} /> : null}
      {workflow ? <WorkflowView workflow={workflow} /> : null}
      {timeline ? <InvestigationTimeline timeline={timeline} /> : null}
      {report ? <FinalReportView report={report} /> : null}
      <RoundHistory loading={roundsLoading} rounds={rounds} />
      {progress || workflow ? <InvestigationTechnicalDetails progress={progress} workflow={workflow} /> : null}
      <footer className="console-footer">创建于 {formatDate(incident.created_at)} · 更新于 {formatDate(incident.updated_at)}</footer>
    </main>
  );
}
