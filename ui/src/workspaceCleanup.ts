// 清理配置不包含时区，由后台主机计算定点计划。
export type WorkspaceCleanupSchedule = {
  enabled: boolean;
  kind: "interval" | "hourly" | "daily" | "weekly";
  interval_value: number;
  interval_unit: "hours" | "days";
  hour: number;
  minute: number;
  weekday: number;
};

export const DEFAULT_WORKSPACE_CLEANUP: WorkspaceCleanupSchedule = {
  enabled: true, kind: "daily", interval_value: 1, interval_unit: "days",
  hour: 6, minute: 0, weekday: 0,
};

export type WorkspaceCleanupStatus = {
  running: boolean;
  scheduler_error?: string | null;
  next_run_at?: number | null;
  next_run_text?: string | null;
  last_run?: {
    status: string;
    started_text?: string | null;
    finished_text?: string | null;
    scanned: number;
    removed: number;
    skipped: number;
    failed: number;
    reclaimed_bytes: number;
    reason?: string;
    details: Array<{ path: string; status: string; reason: string; size_bytes?: number }>;
  } | null;
};

// 文案只描述当前输入；下次执行时间必须来自后台，不能按浏览器时区推算。
export function workspaceCleanupSummary(schedule: WorkspaceCleanupSchedule): string {
  if (!schedule.enabled) return "定时清理已停用";
  if (schedule.kind === "interval") return `每 ${schedule.interval_value} ${schedule.interval_unit === "hours" ? "小时" : "天"}`;
  if (schedule.kind === "hourly") return `每小时第 ${schedule.minute} 分`;
  const clock = `${String(schedule.hour).padStart(2, "0")}:${String(schedule.minute).padStart(2, "0")}`;
  return schedule.kind === "weekly" ? `每周${"一二三四五六日"[schedule.weekday]} ${clock}` : `每天 ${clock}`;
}

export function cleanupResultLabel(status: string): string {
  return ({ running: "清理中", completed: "检查完成", partial: "部分清理失败", failed: "检查失败", interrupted: "已中断", skipped: "已跳过", removed: "已清理" } as Record<string, string>)[status] ?? status;
}
