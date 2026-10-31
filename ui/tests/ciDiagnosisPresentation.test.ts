import assert from "node:assert/strict";
import test from "node:test";
import { diagnosisStatusLabel, shouldRefreshPreflight } from "../src/ciDiagnosisPresentation.ts";
import type { PreflightRunDetail } from "../src/types.ts";

// 仅构造此逻辑读取的字段，覆盖 CI 与诊断跨阶段的刷新边界。
function detail(status: string, diagnosis?: string, processing = false): PreflightRunDetail {
  return {
    status,
    diagnosis: diagnosis ? { status: diagnosis } : null,
    linked_events: [{ status: processing ? "processing" : "completed" }],
  } as PreflightRunDetail;
}

test("CI 结束后等待诊断占位、模型执行和评论写回时继续刷新", () => {
  assert.equal(shouldRefreshPreflight(detail("running")), true);
  assert.equal(shouldRefreshPreflight(detail("waiting_for_space")), true);
  assert.equal(shouldRefreshPreflight(detail("failure", undefined, true)), true);
  assert.equal(shouldRefreshPreflight(detail("failure", "queued")), true);
  assert.equal(shouldRefreshPreflight(detail("failure", "running")), true);
  assert.equal(shouldRefreshPreflight(detail("failure", "completed", true)), true);
});

test("完成、跳过、失败及取消后停止刷新，历史 CI 不无限请求", () => {
  for (const status of ["completed", "skipped", "failed", "timed_out", "cancelled"]) {
    assert.equal(shouldRefreshPreflight(detail("failure", status)), false);
  }
  assert.equal(shouldRefreshPreflight(detail("failure")), false);
  assert.equal(shouldRefreshPreflight(detail("success")), false);
});

test("诊断已完成只表示生成报告，不表示 CI 通过", () => {
  assert.equal(diagnosisStatusLabel("completed"), "报告已生成");
  assert.equal(diagnosisStatusLabel("skipped"), "诊断已跳过");
  assert.equal(diagnosisStatusLabel("failed"), "诊断失败");
});

test("成功 CI 仍在上传附件和回写状态时刷新，回写结束后停止", () => {
  const uploading = { ...detail("success", undefined, true), status_published: false };
  assert.equal(shouldRefreshPreflight(uploading), true);
  assert.equal(shouldRefreshPreflight({ ...uploading, status_published: true }), false);
  assert.equal(shouldRefreshPreflight({ ...uploading, linked_events: [] }), false);
});

test("事件已结束且 CI 终态已回写时，独立附件队列仍触发刷新", () => {
  const published = { ...detail("success"), status_published: true };
  for (const status of ["queued", "running", "completed", "failed", "obsolete"] as const) {
    assert.equal(shouldRefreshPreflight({ ...published, log_delivery: { status, attempts: 1 } }),
      status === "queued" || status === "running");
  }
});
