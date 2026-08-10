export type IncidentStatus =
  | "OPEN"
  | "INVESTIGATING"
  | "WAITING_APPROVAL"
  | "REMEDIATING"
  | "VERIFYING"
  | "RESOLVED"
  | "NEEDS_MANUAL_ACTION";

export interface Incident {
  id: string;
  service: string;
  environment: string;
  description: string;
  status: string;
  time_range_start: string;
  time_range_end: string;
  thread_id: string;
  created_at: string;
  updated_at: string;
}

export interface CreateIncidentInput {
  service: string;
  environment: string;
  description: string;
  time_range_start: string;
  time_range_end: string;
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

export interface WorkflowEvidence {
  id: string;
  evidence_type: string;
  source: string;
  summary: string;
  reference: string | null;
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

export interface WorkflowProposedAction {
  action_type: string;
  summary: string;
  reason: string;
  risk: string;
  supporting_evidence_ids: string[];
}

export interface WorkflowPolicy {
  decision: string;
  reason_code: string;
  reason: string;
  action_id: string | null;
}

export interface WorkflowActionParameters {
  service: string;
  environment: string;
  current_version: string;
  target_version: string;
  reason: string;
}

export interface WorkflowAction {
  action_id: string;
  action_type: string;
  status: string;
  parameters: WorkflowActionParameters;
  executed_at: string | null;
}

export interface WorkflowApprovalOutcome {
  approval_id: string;
  action_id: string;
  status: string;
}

export interface WorkflowExecutionOutcome {
  action_id: string | null;
  approval_id: string | null;
  status: string;
  service: string | null;
  environment: string | null;
  target_version: string | null;
  executed: boolean;
}

export interface WorkflowVerificationOutcome {
  verification_id: string | null;
  action_id: string | null;
  status: string;
  summary: string;
}

export interface WorkflowReportOutcome {
  report_id: string;
  incident_id: string;
  final_status: string;
}

export interface WorkflowResponse {
  incident_id: string;
  incident_status: string;
  current_stage: string;
  hypotheses: WorkflowHypothesis[];
  evidence: WorkflowEvidence[];
  tool_history: WorkflowToolHistory[];
  current_goal: string | null;
  final_conclusion: WorkflowFinalConclusion | null;
  proposed_action: WorkflowProposedAction | null;
  policy_outcome: WorkflowPolicy | null;
  action: WorkflowAction | null;
  approval_outcome: WorkflowApprovalOutcome | null;
  execution_outcome: WorkflowExecutionOutcome | null;
  verification_outcome: WorkflowVerificationOutcome | null;
  report_outcome: WorkflowReportOutcome | null;
  retry_available: boolean;
}

export type ApprovalDecision = "APPROVE" | "REJECT";

export interface FinalReportIncidentSummary {
  incident_id: string;
  service: string;
  environment: string;
  description: string;
  time_range_start: string;
  time_range_end: string;
  final_status: "RESOLVED" | "NEEDS_MANUAL_ACTION";
  created_at: string;
}

export interface FinalReportRootCause {
  summary: string;
  root_cause: string | null;
  confidence: number | null;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  recommended_next_action: string | null;
}

export interface FinalReportHypothesis {
  id: string;
  summary: string;
  status: string;
  confidence: number | null;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  next_check: string | null;
}

export interface FinalReportEvidence {
  id: string;
  evidence_type: string;
  source: string;
  summary: string;
  reference: string | null;
}

export interface FinalReportRecommendedAction {
  action_type: string;
  summary: string;
  reason: string;
  risk: string;
  supporting_evidence_ids: string[];
}

export interface FinalReportAction {
  action_id: string;
  action_type: string;
  status: string;
  parameters: Record<string, unknown>;
  created_at: string;
  executed_at: string | null;
}

export interface FinalReportApproval {
  approval_id: string;
  action_id: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface FinalReportExecution {
  action_id: string | null;
  approval_id: string | null;
  status: string;
  service: string | null;
  environment: string | null;
  target_version: string | null;
  executed: boolean;
}

export interface FinalReportVerification {
  verification_id: string;
  action_id: string;
  status: string;
  summary: string;
  details: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface FinalReportTimelineItem {
  type: string;
  timestamp: string;
  record_id: string;
  summary: string;
}

export interface FinalReportContent {
  schema_version: "v0";
  incident_summary: FinalReportIncidentSummary;
  root_cause: FinalReportRootCause | null;
  hypotheses: FinalReportHypothesis[];
  key_evidence: FinalReportEvidence[];
  recommended_action: FinalReportRecommendedAction | null;
  action: FinalReportAction | null;
  approval: FinalReportApproval | null;
  execution: FinalReportExecution | null;
  verification: FinalReportVerification | null;
  timeline: FinalReportTimelineItem[];
  final_status: "RESOLVED" | "NEEDS_MANUAL_ACTION";
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
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(value));
}
