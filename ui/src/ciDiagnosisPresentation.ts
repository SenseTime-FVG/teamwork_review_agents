import type { PreflightDiagnosis, PreflightRunDetail } from "./types";

export function diagnosisStatusLabel(status?: PreflightDiagnosis["status"]): string {
  // 诊断结果单独表达，不能把“报告已生成”显示为 CI 通过。
  return status ? {
    queued: "等待诊断", running: "正在诊断", completed: "报告已生成",
    failed: "诊断失败", timed_out: "诊断超时", cancelled: "诊断已取消", skipped: "诊断已跳过",
  }[status] : "尚未生成诊断报告";
}

export function shouldRefreshPreflight(detail: PreflightRunDetail): boolean {
  // CI 结束与诊断占位之间也继续刷新，避免恰好读到空报告后永久停止轮询。
  return detail.status === "running"
    || detail.diagnosis?.status === "queued"
    || detail.diagnosis?.status === "running"
    || (["failure", "timed_out", "error"].includes(detail.status)
      && detail.linked_events.some(event => event.status === "processing"));
}

export async function copyDiagnosisMarkdown(text: string): Promise<void> {
  // 局域网 HTTP 页面可能没有 Clipboard API，使用用户点击触发的兼容复制。
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(text); return; } catch { /* 继续兼容复制。 */ }
  }
  const focused = document.activeElement;
  const field = document.createElement("textarea");
  field.value = text;
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.append(field);
  try {
    field.select();
    if (!document.execCommand("copy")) throw new Error("复制失败，请选中报告内容复制");
  } finally {
    field.remove();
    if (focused instanceof HTMLElement) focused.focus();
  }
}
