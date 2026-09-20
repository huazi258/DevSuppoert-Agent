"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";

import { ApiError, createIncident, listInvestigationTargets } from "../lib/api";
import type { InvestigationTargetOption } from "../lib/types";

function localDateTimeValue(date: Date): string {
  const offsetDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return offsetDate.toISOString().slice(0, 16);
}

function messageFor(error: unknown): string {
  if (error instanceof ApiError && error.status === 0) {
    return error.detail;
  }
  return "创建故障调查失败，请检查填写内容后重试。";
}

const capabilityLabels: Record<string, string> = {
  logs: "日志",
  metrics: "指标",
  traces: "链路",
  deployment_facts: "部署事实",
};

export function IncidentCreateForm() {
  const router = useRouter();
  const [targets, setTargets] = useState<InvestigationTargetOption[]>([]);
  const [targetId, setTargetId] = useState("");
  const [serviceId, setServiceId] = useState("");
  const [timeRangeStart, setTimeRangeStart] = useState("");
  const [timeRangeEnd, setTimeRangeEnd] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [targetsLoading, setTargetsLoading] = useState(true);
  const [pending, setPending] = useState(false);

  const selectedTarget = useMemo(
    () => targets.find((target) => target.id === targetId) ?? null,
    [targetId, targets],
  );

  useEffect(() => {
    const end = new Date();
    const start = new Date(end.getTime() - 15 * 60_000);
    setTimeRangeStart(localDateTimeValue(start));
    setTimeRangeEnd(localDateTimeValue(end));
  }, []);

  useEffect(() => {
    async function loadTargets() {
      setTargetsLoading(true);
      try {
        setTargets(await listInvestigationTargets());
        setError(null);
      } catch (loadError: unknown) {
        setTargets([]);
        setError(
          loadError instanceof ApiError && loadError.status === 0
            ? loadError.detail
            : "调查目标加载失败，请稍后重试。",
        );
      } finally {
        setTargetsLoading(false);
      }
    }

    void loadTargets();
  }, []);

  function onTargetChange(nextTargetId: string) {
    setTargetId(nextTargetId);
    setServiceId("");
    setError(null);
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedDescription = description.trim();
    if (!targetId) {
      setError("请选择调查目标。");
      return;
    }
    if (!serviceId) {
      setError("请选择受影响服务。");
      return;
    }
    if (!timeRangeStart || !timeRangeEnd || !normalizedDescription) {
      setError("请完整填写时间范围和观察到的现象。");
      return;
    }
    if (new Date(timeRangeEnd) < new Date(timeRangeStart)) {
      setError("结束时间不得早于开始时间。");
      return;
    }

    setPending(true);
    setError(null);
    try {
      const incident = await createIncident({
        target_id: targetId,
        service_id: serviceId,
        description: normalizedDescription,
        time_range_start: new Date(timeRangeStart).toISOString(),
        time_range_end: new Date(timeRangeEnd).toISOString(),
      });
      router.push(`/incidents/${incident.id}`);
    } catch (submissionError: unknown) {
      setError(messageFor(submissionError));
    } finally {
      setPending(false);
    }
  }

  const serviceUnavailable = selectedTarget !== null && selectedTarget.services.length === 0;

  return (
    <section className="panel" aria-labelledby="create-incident-heading">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">新的调查</p>
          <h2 id="create-incident-heading">创建故障调查</h2>
        </div>
      </div>
      <form className="incident-form" onSubmit={onSubmit}>
        <label className="full-width">
          调查目标
          <select
            disabled={targetsLoading || pending}
            onChange={(event) => onTargetChange(event.target.value)}
            value={targetId}
          >
            <option value="">{targetsLoading ? "正在加载调查目标…" : "请选择调查目标"}</option>
            {targets.map((target) => (
              <option key={target.id} value={target.id}>
                {target.display_name}（{target.environment}）
              </option>
            ))}
          </select>
        </label>
        {selectedTarget ? (
          <p className="subtle-status full-width">
            环境：{selectedTarget.environment}
            {selectedTarget.capabilities.length > 0
              ? `；可用能力：${selectedTarget.capabilities
                  .map((capability) => capabilityLabels[capability] ?? capability)
                  .join("、")}`
              : ""}
          </p>
        ) : null}
        <label className="full-width">
          受影响服务
          <select
            disabled={!selectedTarget || serviceUnavailable || pending}
            onChange={(event) => setServiceId(event.target.value)}
            value={serviceId}
          >
            <option value="">
              {!selectedTarget
                ? "请先选择调查目标"
                : serviceUnavailable
                  ? "该调查目标没有可用服务"
                  : "请选择受影响服务"}
            </option>
            {selectedTarget?.services.map((service) => (
              <option key={service.id} value={service.id}>
                {service.display_name}（{service.name}）
              </option>
            ))}
          </select>
        </label>
        {serviceUnavailable ? <p className="error-banner full-width">该调查目标没有可用服务。</p> : null}
        <label>
          开始时间
          <input
            required
            type="datetime-local"
            value={timeRangeStart}
            onChange={(event) => setTimeRangeStart(event.target.value)}
          />
        </label>
        <label>
          结束时间
          <input
            required
            type="datetime-local"
            value={timeRangeEnd}
            onChange={(event) => setTimeRangeEnd(event.target.value)}
          />
        </label>
        <label className="full-width">
          观察到的现象
          <textarea
            required
            rows={4}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="请描述实际观察到的服务行为、报错或影响。"
          />
        </label>
        {error ? <p className="error-banner full-width" role="alert">{error}</p> : null}
        <button
          className="button primary-button full-width"
          disabled={pending || targetsLoading || !targetId || !serviceId || serviceUnavailable}
          type="submit"
        >
          {pending ? "正在创建…" : "创建新的故障调查"}
        </button>
      </form>
    </section>
  );
}
