export type IncidentStatus =
  | "OPEN"
  | "INVESTIGATING"
  | "CONCLUDED"
  | "INCONCLUSIVE"
  | "FAILED";

export interface InvestigationServiceOption {
  id: string;
  name: string;
  display_name: string;
}

export interface InvestigationTargetOption {
  id: string;
  display_name: string;
  environment: string;
  capabilities: string[];
  services: InvestigationServiceOption[];
}

export interface Incident {
  id: string;
  target_id: string;
  service_id: string;
  service: string;
  environment: string;
  description: string;
  status: IncidentStatus;
  time_range_start: string;
  time_range_end: string;
  thread_id: string;
  created_at: string;
  updated_at: string;
}

export interface CreateIncidentInput {
  target_id: string;
  service_id: string;
  description: string;
  time_range_start: string;
  time_range_end: string;
}

export interface SupplementalObservationInput {
  content: string;
  observed_at?: string;
}

export interface SupplementalObservation {
  id: string;
  content: string;
  observed_at: string;
}

export interface InvestigationContinuationResponse {
  incident_id: string;
  round_id: string;
  round_number: number;
  status: IncidentStatus;
  observation: SupplementalObservation;
  previous_round_id: string;
  accepted: true;
}

export interface InvestigationRoundReportSummary {
  id: string;
  conclusion_summary: string | null;
  final_status: IncidentStatus | null;
}

export interface InvestigationRound {
  round_id: string;
  round_number: number;
  status: IncidentStatus;
  thread_id: string;
  started_at: string;
  completed_at: string | null;
  terminal_reason: string | null;
  triggering_observation: SupplementalObservation | null;
  report: InvestigationRoundReportSummary | null;
  is_current: boolean;
}

export interface WorkflowHypothesis {
  id: string;
  summary: string;
  status: string;
  confidence: number | null;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  next_check: string | null;
}

export interface WorkflowEvidenceCitation {
  id: string;
  document_id: string;
  chunk_id: string;
  document_title: string;
  source: string;
  section: string;
  document_reference: string;
}

export interface WorkflowEvidence {
  id: string;
  evidence_type: string;
  source: string;
  summary: string;
  reference: string | null;
  citation: WorkflowEvidenceCitation | null;
}

export interface WorkflowToolError {
  code: string;
  message: string;
  retryable: boolean;
}

export interface WorkflowToolHistory {
  tool_name: string;
  tool_arguments: Record<string, unknown>;
  status: string;
  duration_ms: number | null;
  evidence_ids: string[];
  error: WorkflowToolError | null;
}

export interface WorkflowFinalConclusion {
  summary: string;
  root_cause: string | null;
  confidence: number | null;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  recommended_next_action: string | null;
}

export interface WorkflowReportOutcome {
  report_id: string;
  incident_id: string;
  final_status: IncidentStatus;
}

export interface WorkflowResponse {
  incident_id: string;
  incident_status: IncidentStatus;
  current_stage: string;
  hypotheses: WorkflowHypothesis[];
  evidence: WorkflowEvidence[];
  tool_history: WorkflowToolHistory[];
  current_goal: string | null;
  final_conclusion: WorkflowFinalConclusion | null;
  report_outcome: WorkflowReportOutcome | null;
  terminal_reason: string | null;
  retry_available: boolean;
}

export interface WorkflowStartResponse {
  incident_id: string;
  incident_status: IncidentStatus;
  accepted: true;
}

export type WorkflowProgressPhase = "not_started" | "accepted" | "running" | "failed" | "completed";

export interface WorkflowProgressLatestTool {
  tool_name: string;
  status: string;
  duration_ms: number | null;
}

export interface WorkflowProgressFailure {
  failed_node: string;
  category: string;
  message: string;
  retryable: boolean;
}

export interface WorkflowProgressResponse {
  incident_id: string;
  incident_status: IncidentStatus;
  phase: WorkflowProgressPhase;
  checkpoint_available: boolean;
  current_stage: string | null;
  current_goal: string | null;
  pending_tool_name: string | null;
  hypothesis_count: number;
  evidence_count: number;
  tool_call_count: number;
  investigation_round: number;
  llm_call_count: number;
  workflow_retry_count: number;
  latest_tool: WorkflowProgressLatestTool | null;
  failure: WorkflowProgressFailure | null;
  terminal_reason: string | null;
  retry_available: boolean;
}

export type InvestigationTimelineEventType =
  | "investigation_started"
  | "knowledge_searched"
  | "hypothesis_created"
  | "evidence_collected"
  | "hypothesis_updated"
  | "conclusion_reached"
  | "investigation_interrupted"
  | "investigation_completed";

export interface InvestigationTimelineEvent {
  event_id: string;
  sequence: number;
  event_type: InvestigationTimelineEventType;
  occurred_at: string | null;
  title: string;
  summary: string;
  status: string | null;
}

export interface WorkflowTimelineResponse {
  incident_id: string;
  checkpoint_available: boolean;
  truncated: boolean;
  events: InvestigationTimelineEvent[];
}

export interface FinalReportInputSummary {
  service: string;
  environment: string;
  description: string;
  time_range_start: string;
  time_range_end: string;
}

export interface FinalReportConclusion {
  summary: string;
  root_cause: string | null;
  confidence: number | null;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  citations: WorkflowEvidenceCitation[];
}

export interface FinalReportEvidence {
  id: string;
  source: string;
  summary: string;
  reference: string | null;
  citation: WorkflowEvidenceCitation | null;
}

export interface FinalReportTimelineItem {
  event: string;
  summary: string;
}

export interface FinalReportContent {
  schema_version: "v2";
  input_summary: FinalReportInputSummary;
  conclusion: FinalReportConclusion | null;
  hypotheses: WorkflowHypothesis[];
  key_evidence: FinalReportEvidence[];
  unknowns: string[];
  manual_suggestions: string[];
  terminal_reason: string | null;
  timeline: FinalReportTimelineItem[];
  final_status: IncidentStatus;
}

export interface FinalReport {
  id: string;
  incident_id: string;
  root_cause: string | null;
  content: FinalReportContent;
  created_at: string;
  updated_at: string;
}

export function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(value));
}
