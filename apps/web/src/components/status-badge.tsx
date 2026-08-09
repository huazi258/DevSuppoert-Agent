interface StatusBadgeProps {
  value: string;
}

const statusGroups: Record<string, string> = {
  OPEN: "status-open",
  INVESTIGATING: "status-active",
  WAITING_APPROVAL: "status-pending",
  REMEDIATING: "status-active",
  VERIFYING: "status-active",
  RESOLVED: "status-success",
  NEEDS_MANUAL_ACTION: "status-warning",
  ACTIVE: "status-active",
  SUPPORTED: "status-supported",
  REJECTED: "status-rejected",
  CONFIRMED: "status-success",
  PASS: "status-success",
  FAIL: "status-rejected",
  INCONCLUSIVE: "status-warning",
};

export function StatusBadge({ value }: StatusBadgeProps) {
  return <span className={`status-badge ${statusGroups[value] ?? "status-neutral"}`}>{value}</span>;
}
