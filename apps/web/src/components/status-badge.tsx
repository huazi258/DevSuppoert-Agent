interface StatusBadgeProps {
  value: string;
}

const statusGroups: Record<string, string> = {
  OPEN: "status-open",
  INVESTIGATING: "status-active",
  CONCLUDED: "status-success",
  INCONCLUSIVE: "status-warning",
  FAILED: "status-rejected",
};

const statusLabels: Record<string, string> = {
  OPEN: "待调查",
  INVESTIGATING: "调查中",
  CONCLUDED: "已形成结论",
  INCONCLUSIVE: "证据不足，暂无法确认",
  FAILED: "调查失败",
};

export function StatusBadge({ value }: StatusBadgeProps) {
  return <span className={`status-badge ${statusGroups[value] ?? "status-neutral"}`}>{statusLabels[value] ?? value}</span>;
}
