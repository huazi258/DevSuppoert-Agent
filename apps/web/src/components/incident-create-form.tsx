"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";

import { ApiError, createIncident } from "../lib/api";

function localDateTimeValue(date: Date): string {
  const offsetDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return offsetDate.toISOString().slice(0, 16);
}

function messageFor(error: unknown): string {
  return error instanceof ApiError ? error.detail : "Unable to create the Incident.";
}

export function IncidentCreateForm() {
  const router = useRouter();
  const [service, setService] = useState("order-service");
  const [environment, setEnvironment] = useState("local");
  const [timeRangeStart, setTimeRangeStart] = useState("");
  const [timeRangeEnd, setTimeRangeEnd] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    const end = new Date();
    const start = new Date(end.getTime() - 15 * 60_000);
    setTimeRangeStart(localDateTimeValue(start));
    setTimeRangeEnd(localDateTimeValue(end));
  }, []);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedDescription = description.trim();
    if (!service || !environment || !timeRangeStart || !timeRangeEnd || !normalizedDescription) {
      setError("Complete all Incident fields before submitting.");
      return;
    }
    if (new Date(timeRangeEnd) < new Date(timeRangeStart)) {
      setError("End time must be after or equal to start time.");
      return;
    }

    setPending(true);
    setError(null);
    try {
      const incident = await createIncident({
        service,
        environment,
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

  return (
    <section className="panel" aria-labelledby="create-incident-heading">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">New incident</p>
          <h2 id="create-incident-heading">Create Incident</h2>
        </div>
      </div>
      <form className="incident-form" onSubmit={onSubmit}>
        <label>
          Service
          <select value={service} onChange={(event) => setService(event.target.value)}>
            <option value="order-service">order-service</option>
            <option value="payment-service">payment-service</option>
          </select>
        </label>
        <label>
          Environment
          <select value={environment} onChange={(event) => setEnvironment(event.target.value)}>
            <option value="local">local</option>
            <option value="production">production</option>
          </select>
        </label>
        <label>
          Start time
          <input
            required
            type="datetime-local"
            value={timeRangeStart}
            onChange={(event) => setTimeRangeStart(event.target.value)}
          />
        </label>
        <label>
          End time
          <input
            required
            type="datetime-local"
            value={timeRangeEnd}
            onChange={(event) => setTimeRangeEnd(event.target.value)}
          />
        </label>
        <label className="full-width">
          Description
          <textarea
            required
            rows={4}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="Describe the observed service behavior."
          />
        </label>
        {error ? <p className="error-banner" role="alert">{error}</p> : null}
        <button className="button primary-button" disabled={pending} type="submit">
          {pending ? "Creating…" : "Create Incident"}
        </button>
      </form>
    </section>
  );
}
