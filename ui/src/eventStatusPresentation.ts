import type { EventRecord } from "./types";

// 筛选和列表共享进行中语义；内部状态不变，不新增 CI 阶段状态。
export const EVENT_STATUS_OPTIONS = [
  { value: "pending", label: "待处理" },
  { value: "processing", label: "处理中" },
  { value: "unmatched", label: "未触发" },
  { value: "triggered", label: "已触发" },
  { value: "completed", label: "已处理" },
  { value: "failed", label: "处理失败" },
  { value: "cancelled", label: "已取消" },
];

export function unmatchedReasonLabel(reason?: string | null): string | null {
  if (!reason) return null;
  const labels: Record<string, string> = {
    scan_deduplicated: "本扫描周期内已被更新事件替代",
  };
  return labels[reason] ?? reason;
}

// CI 等待、准备及执行均显示“处理中”；终态继续保留门禁失败等真实结论。
export function eventStatusPresentation(event: EventRecord): {
  label: string;
  visualStatus: string;
  details?: string;
} {
  const labels: Record<string, string> = {
    ...Object.fromEntries(EVENT_STATUS_OPTIONS.map(({ value, label }) => [value, label])),
    completed: event.trigger_count > 0 ? "已处理" : "已结束",
  };
  let label = labels[event.status] ?? event.status;
  let visualStatus = event.status;
  if (event.error?.includes("状态回写失败")) {
    label = "状态回写失败";
    visualStatus = "failed";
  } else if (event.status === "completed" && event.preflight_status === "failure") {
    label = "本地 CI 未通过";
    visualStatus = "failed";
  } else if (event.status === "completed" && event.preflight_status === "timed_out") {
    label = "本地 CI 超时";
    visualStatus = "failed";
  } else if (event.status === "completed" && event.preflight_status === "superseded") {
    label = "Head 已更新，已跳过";
    visualStatus = "unmatched";
  } else if (event.status === "failed" && event.preflight_status === "error") {
    label = "本地 CI 异常";
  }
  const details = event.error
    ?? event.preflight_error
    ?? unmatchedReasonLabel(event.unmatched_reason)
    ?? (event.preflight_failed_step ? `失败步骤：${event.preflight_failed_step}` : undefined);
  return { label, visualStatus, details };
}
