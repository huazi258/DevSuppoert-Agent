"use client";

import { useState, type FormEvent } from "react";

import type { SupplementalObservationInput } from "../lib/types";

interface ContinuationFormProps {
  onContinue: (input: SupplementalObservationInput) => Promise<boolean>;
}

function localDateTimeValue(date: Date): string {
  const offsetDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return offsetDate.toISOString().slice(0, 16);
}

export function ContinuationForm({ onContinue }: ContinuationFormProps) {
  const [content, setContent] = useState("");
  const [observedAt, setObservedAt] = useState(() => localDateTimeValue(new Date()));
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedContent = content.trim();
    if (!normalizedContent) {
      setError("请填写新观察到的现象。");
      return;
    }
    setPending(true);
    setError(null);
    try {
      const accepted = await onContinue({
        content: normalizedContent,
        ...(observedAt ? { observed_at: new Date(observedAt).toISOString() } : {}),
      });
      if (accepted) {
        setContent("");
        setObservedAt(localDateTimeValue(new Date()));
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="panel" aria-labelledby="continuation-heading">
      <p className="eyebrow">补充观察</p>
      <h2 id="continuation-heading">补充信息并继续调查</h2>
      <p className="subtle-status">此处填写的是待验证观察，不是已确认事实，也不代表故障已被处理。</p>
      <form className="incident-form" onSubmit={onSubmit}>
        <label className="full-width">
          新观察到的现象
          <textarea
            required
            rows={4}
            value={content}
            onChange={(event) => setContent(event.target.value)}
            placeholder="请描述新出现的现象、影响或上下文。"
          />
        </label>
        <label className="full-width">
          发生时间（可选）
          <input
            type="datetime-local"
            value={observedAt}
            onChange={(event) => setObservedAt(event.target.value)}
          />
        </label>
        {error ? <p className="error-banner full-width" role="alert">{error}</p> : null}
        <button className="button primary-button full-width" disabled={pending} type="submit">
          {pending ? "正在创建新一轮调查…" : "补充信息并继续调查"}
        </button>
      </form>
    </section>
  );
}
