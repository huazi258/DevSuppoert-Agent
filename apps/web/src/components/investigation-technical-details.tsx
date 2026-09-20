import type { WorkflowProgressResponse, WorkflowResponse } from "../lib/types";
import { StatusBadge } from "./status-badge";

interface InvestigationTechnicalDetailsProps {
  progress: WorkflowProgressResponse | null;
  workflow: WorkflowResponse | null;
}

function yesNo(value: boolean): string {
  return value ? "是" : "否";
}

function duration(value: number | null): string {
  return value === null ? "未记录" : `${value.toFixed(0)} ms`;
}

function ToolCalls({ workflow }: { workflow: WorkflowResponse }) {
  return (
    <section className="technical-section" aria-labelledby="technical-tool-calls-heading">
      <h3 id="technical-tool-calls-heading">Tool 调用</h3>
      {workflow.tool_history.length === 0 ? <p className="empty-state">尚未记录 Tool 调用。</p> : (
        <ol className="technical-tool-list">
          {workflow.tool_history.map((tool, index) => (
            <li className="technical-tool-call" key={`${tool.tool_name}-${index}`}>
              <div className="record-header"><strong className="mono">{tool.tool_name}</strong><StatusBadge value={tool.status} /></div>
              <dl className="technical-facts">
                <div><dt>耗时</dt><dd>{duration(tool.duration_ms)}</dd></div>
                <div><dt>证据 ID</dt><dd className="mono">{tool.evidence_ids.join(", ") || "无"}</dd></div>
              </dl>
              <p className="technical-label">结构化参数</p>
              <pre>{JSON.stringify(tool.tool_arguments, null, 2)}</pre>
              {tool.error ? (
                <dl className="technical-facts technical-error">
                  <div><dt>安全错误代码</dt><dd>{tool.error.code}</dd></div>
                  <div><dt>安全错误说明</dt><dd>{tool.error.message}</dd></div>
                  <div><dt>可重试</dt><dd>{yesNo(tool.error.retryable)}</dd></div>
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
  const awaitingFirstCheckpoint = progress?.phase === "accepted" && !progress.checkpoint_available;

  return (
    <details className="technical-details">
      <summary>技术详情</summary>
      <p className="technical-description">此处展示已持久化的调查阶段、预算计数、Tool 调用和安全失败信息。</p>

      {progress ? (
        <section className="technical-section" aria-labelledby="technical-workflow-execution-heading">
          <h3 id="technical-workflow-execution-heading">运行状态</h3>
          {awaitingFirstCheckpoint ? (
            <p>首个检查点尚未持久化。</p>
          ) : (
            <dl className="technical-facts">
              {progress.current_stage ? <div><dt>当前 stage</dt><dd className="mono">{progress.current_stage}</dd></div> : null}
              {progress.pending_tool_name ? <div><dt>待执行 Tool</dt><dd className="mono">{progress.pending_tool_name}</dd></div> : null}
              <div><dt>调查轮次</dt><dd>{progress.investigation_round}</dd></div>
              <div><dt>Tool 调用次数</dt><dd>{progress.tool_call_count}</dd></div>
              <div><dt>LLM 调用次数</dt><dd>{progress.llm_call_count}</dd></div>
              <div><dt>工作流重试次数</dt><dd>{progress.workflow_retry_count}</dd></div>
              <div><dt>检查点可用</dt><dd>{yesNo(progress.checkpoint_available)}</dd></div>
              <div><dt>允许重试</dt><dd>{yesNo(progress.retry_available)}</dd></div>
              {progress.latest_tool ? <div><dt>最近 Tool</dt><dd><span className="mono">{progress.latest_tool.tool_name}</span> · {progress.latest_tool.status} · {duration(progress.latest_tool.duration_ms)}</dd></div> : null}
              {progress.terminal_reason ? <div><dt>终态原因</dt><dd className="mono">{progress.terminal_reason}</dd></div> : null}
              {progress.failure ? (
                <>
                  <div><dt>失败分类</dt><dd className="mono">{progress.failure.category}</dd></div>
                  <div><dt>失败节点</dt><dd className="mono">{progress.failure.failed_node}</dd></div>
                  <div><dt>安全失败说明</dt><dd>{progress.failure.message}</dd></div>
                  <div><dt>失败可重试</dt><dd>{yesNo(progress.failure.retryable)}</dd></div>
                </>
              ) : null}
            </dl>
          )}
        </section>
      ) : workflow ? (
        <section className="technical-section" aria-labelledby="technical-workflow-execution-heading">
          <h3 id="technical-workflow-execution-heading">运行状态</h3>
          <dl className="technical-facts"><div><dt>当前 stage</dt><dd className="mono">{workflow.current_stage}</dd></div></dl>
        </section>
      ) : null}

      {workflow ? <ToolCalls workflow={workflow} /> : null}
    </details>
  );
}
