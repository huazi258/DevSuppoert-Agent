"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ApiError, listIncidents } from "../lib/api";
import { formatDate, type Incident } from "../lib/types";
import { StatusBadge } from "./status-badge";

function messageFor(error: unknown): string {
  return error instanceof ApiError ? error.detail : "Unable to load Incidents.";
}

export function IncidentList() {
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const loadIncidents = useCallback(async () => {
    setLoading(true);
    try {
      setIncidents(await listIncidents());
      setError(null);
    } catch (loadError: unknown) {
      setError(messageFor(loadError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadIncidents();
  }, [loadIncidents]);

  return (
    <section className="panel" aria-labelledby="incident-list-heading">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Persisted records</p>
          <h2 id="incident-list-heading">Incident List</h2>
        </div>
        <button className="button secondary-button" onClick={() => void loadIncidents()} type="button">
          Refresh
        </button>
      </div>
      {error ? <p className="error-banner" role="alert">{error}</p> : null}
      {loading && incidents.length === 0 ? <p className="empty-state">Loading Incidents…</p> : null}
      {!loading && incidents.length === 0 && !error ? <p className="empty-state">No incidents yet.</p> : null}
      {incidents.length > 0 ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Service</th>
                <th>Environment</th>
                <th>Status</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {incidents.map((incident) => (
                <tr key={incident.id}>
                  <td><Link href={`/incidents/${incident.id}`}>{incident.service}</Link></td>
                  <td>{incident.environment}</td>
                  <td><StatusBadge value={incident.status} /></td>
                  <td>{formatDate(incident.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
