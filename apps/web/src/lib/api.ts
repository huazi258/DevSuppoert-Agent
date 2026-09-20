import type {
  CreateIncidentInput,
  FinalReport,
  Incident,
  IncidentStatus,
  InvestigationTargetOption,
  WorkflowResponse,
  WorkflowProgressResponse,
  WorkflowStartResponse,
  WorkflowTimelineResponse,
} from "./types";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_DEVSUPPORT_API_BASE_URL ?? "http://127.0.0.1:8002";

type IncidentApiResponse = Omit<Incident, "status"> & {
  status: string;
  investigation_status: IncidentStatus;
};

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
    throw new ApiError(0, "无法连接到 DevSupport 服务。");
  }

  const payload: unknown = response.status === 204 ? undefined : await response.json().catch(() => undefined);
  if (!response.ok) {
    throw new ApiError(response.status, errorDetail(payload, `请求失败（${response.status}）。`));
  }
  return payload as T;
}

function toV2Incident(response: IncidentApiResponse): Incident {
  const { investigation_status: status, ...incident } = response;
  return { ...incident, status };
}

export async function listIncidents(): Promise<Incident[]> {
  return (await request<IncidentApiResponse[]>("/incidents")).map(toV2Incident);
}

export function listInvestigationTargets(): Promise<InvestigationTargetOption[]> {
  return request<InvestigationTargetOption[]>("/incidents/investigation-targets");
}

export async function createIncident(input: CreateIncidentInput): Promise<Incident> {
  const response = await request<IncidentApiResponse>("/incidents", {
    method: "POST",
    body: JSON.stringify(input),
  });
  return toV2Incident(response);
}

export async function getIncident(id: string): Promise<Incident> {
  return toV2Incident(await request<IncidentApiResponse>(`/incidents/${id}`));
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

export function getWorkflowTimeline(id: string): Promise<WorkflowTimelineResponse> {
  return request<WorkflowTimelineResponse>(`/incidents/${id}/workflow/timeline`);
}

export function getFinalReport(id: string): Promise<FinalReport> {
  return request<FinalReport>(`/incidents/${id}/report`);
}
