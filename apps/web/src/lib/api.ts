import type {
  ApprovalDecision,
  CreateIncidentInput,
  FinalReport,
  Incident,
  WorkflowResponse,
  WorkflowProgressResponse,
  WorkflowStartResponse,
} from "./types";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_DEVSUPPORT_API_BASE_URL ?? "http://127.0.0.1:8002";

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function errorDetail(payload: unknown, fallback: string): string {
  if (
    typeof payload === "object" &&
    payload !== null &&
    "detail" in payload &&
    typeof payload.detail === "string"
  ) {
    return payload.detail;
  }
  return fallback;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      cache: "no-store",
      headers,
    });
  } catch {
    throw new ApiError(0, "Unable to reach the DevSupport API.");
  }

  const payload: unknown = response.status === 204 ? undefined : await response.json().catch(() => undefined);
  if (!response.ok) {
    throw new ApiError(response.status, errorDetail(payload, `Request failed (${response.status}).`));
  }
  return payload as T;
}

export function listIncidents(): Promise<Incident[]> {
  return request<Incident[]>("/incidents");
}

export function createIncident(input: CreateIncidentInput): Promise<Incident> {
  return request<Incident>("/incidents", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getIncident(id: string): Promise<Incident> {
  return request<Incident>(`/incidents/${id}`);
}

export function startWorkflow(id: string): Promise<WorkflowStartResponse> {
  return request<WorkflowStartResponse>(`/incidents/${id}/workflow`, { method: "POST" });
}

export function retryWorkflow(id: string): Promise<WorkflowResponse> {
  return request<WorkflowResponse>(`/incidents/${id}/workflow/retry`, { method: "POST" });
}

export function getWorkflow(id: string): Promise<WorkflowResponse> {
  return request<WorkflowResponse>(`/incidents/${id}/workflow`);
}

export function getWorkflowProgress(id: string): Promise<WorkflowProgressResponse> {
  return request<WorkflowProgressResponse>(`/incidents/${id}/workflow/progress`);
}

export async function submitApproval(id: string, decision: ApprovalDecision): Promise<void> {
  await request<unknown>(`/incidents/${id}/approval`, {
    method: "POST",
    body: JSON.stringify({ decision }),
  });
}

export function getFinalReport(id: string): Promise<FinalReport> {
  return request<FinalReport>(`/incidents/${id}/report`);
}
