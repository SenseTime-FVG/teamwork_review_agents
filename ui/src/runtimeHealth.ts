import type { RuntimeStatus } from "./types.ts";

export type RuntimeAlert = {
  key: string;
  message: string;
  failedAt?: number;
  retryAt?: number | null;
  recovering?: boolean;
};

// 后台故障优先于扫描中和暂停，避免进程在线掩盖实际任务已经停止。
export function runtimeHealth(status: RuntimeStatus) {
  const alerts: RuntimeAlert[] = [];
  const messages = new Set<string>();
  const labels: Record<string, string> = { scanner: "仓库扫描", dispatcher: "Agent 调度", scheduler: "定时调度", ci_logs: "CI 日志附件" };
  for (const [worker, task] of Object.entries(status.background_tasks ?? {})) {
    if (!task.error && !["retrying", "recovering", "stopped"].includes(task.state)) continue;
    const message = task.error || "后台任务已停止";
    messages.add(message);
    alerts.push({
      key: worker,
      message: `${labels[worker] || worker}异常：${message}`,
      failedAt: task.failed_at,
      retryAt: task.retry_at,
      recovering: task.state === "recovering",
    });
  }
  for (const [key, label, message] of [
    ["config", "配置错误", status.config_error],
    ["scan", "扫描失败", status.last_error],
    ["dispatch", "Agent 调度失败", status.last_dispatch_error],
    ["schedule", "定时任务失败", status.last_schedule_error],
    ["status", "后台状态读写失败", status.status_error],
  ] as const) {
    if (!message || messages.has(message)) continue;
    messages.add(message);
    alerts.push({ key, message: `${label}：${message}` });
  }
  const hasError = alerts.length > 0;
  const title = hasError ? "后台运行异常" : status.paused ? "扫描已暂停" : status.running_cycle ? "正在扫描" : status.dispatching_events ? "Agent 调度进行中" : "服务运行正常";
  const scanner = status.background_tasks?.scanner;
  const scanFailed = Boolean(status.last_error || scanner?.error || scanner?.state === "stopped");
  return {
    alerts,
    hasError,
    title,
    sidebarLabel: hasError ? "后台异常" : status.paused ? "已暂停" : "后台在线",
    scanLabel: scanFailed ? "异常" : status.running_cycle ? "进行中" : "已结束",
  };
}
