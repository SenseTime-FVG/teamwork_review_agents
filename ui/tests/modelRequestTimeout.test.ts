import assert from "node:assert/strict";
import test from "node:test";
import { presentRunLogs } from "../src/runLogPresentation.ts";
import type { RunLog } from "../src/types.ts";

// 同一运行的模拟日志；不依赖后台或真实任务。
function log(id: number, event_type: string, payload: unknown): RunLog {
  return { id, run_id: "test", created_at: id, stream: "system", event_type, payload: JSON.stringify(payload) };
}

test("按实际错误码区分超时，不把请求、CI 或 idle 超时显示为总时限", () => {
  const codes = ["agent_idle_timeout", "agent_total_timeout", "remote_ci_timeout", "model_request_timeout", "unknown"];
  const messages = presentRunLogs(codes.map((error_code, i) => log(i, "run.timed_out", { error_code })));
  assert.deepEqual(messages.map(m => m.title), [
    "运行因无进展而超时", "运行超过总时限", "等待远端 CI 超时", "模型请求恢复耗尽，运行终止", "运行超时",
  ]);
});

test("旧 idle 停止和跨清理事件的终态汇总合并显示，原始记录保留", () => {
  const messages = presentRunLogs([
    log(1, "run.idle_timed_out", "Agent 长时间没有模型或工具进展"),
    log(2, "run.home_cleaned", { path: "/tmp/test" }),
    log(3, "run.timed_out", { error_code: "agent_idle_timeout", error: "Agent 长时间没有模型或工具进展" }),
  ]);
  assert.equal(messages.length, 2);
  assert.equal(messages[0].title, "运行因无进展而超时");
  assert.equal(messages[0].repeatCount, 1);
  assert.equal(JSON.parse(messages[0].raw).length, 2);
});

test("不同错误和不同运行的超时不会被合并", () => {
  const messages = presentRunLogs([
    log(1, "run.idle_timed_out", "无进展"),
    log(2, "run.timed_out", { error_code: "agent_total_timeout", error: "总时限" }),
    { ...log(3, "run.timed_out", { error_code: "agent_idle_timeout", error: "无进展" }), run_id: "other" },
  ]);
  assert.equal(messages.length, 3);
});

test("重复底层超时日志之后的终态仍能正确合并", () => {
  const messages = presentRunLogs([
    log(1, "run.idle_timed_out", "无进展"),
    log(2, "run.idle_timed_out", "无进展"),
    log(3, "run.timed_out", { error_code: "agent_idle_timeout", error: "无进展" }),
  ]);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].title, "运行因无进展而超时");
});

test("请求超时阶段展示候选、时限与上游信息，不宣称整轮运行失败", () => {
  const [message] = presentRunLogs([log(1, "model.request_timed_out", {
    request_round: 15, request_attempt: 1, provider_id: "codex-cli", model: "test-model",
    stage: "reading_response", elapsed_seconds: 300, idle_seconds: 300,
    idle_timeout_seconds: 300, request_timeout_seconds: 1200, timeout_kind: "idle",
    http_status: 200, upstream_request_id: "req-test", phase: "inference",
  })]);
  assert.equal(message.kind, "warning");
  assert.equal(message.body, "读取响应内容");
  assert.match(message.detail, /第 15 轮 · codex-cli \/ test-model/);
  assert.match(message.detail, /无响应上限 300 秒/);
  assert.match(message.detail, /请求最长 1200 秒/);
  assert.match(message.detail, /HTTP 状态：200/);
  assert.match(message.detail, /req-test/);
});
