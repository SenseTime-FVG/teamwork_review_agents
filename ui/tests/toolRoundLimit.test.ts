// 轮数预算、继承说明和未完成终态的展示回归。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { presentRunLogs } from "../src/runLogPresentation.ts";

test("全局默认 256，Agent 可覆盖或留空继承具体轮数，保留原有时限", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.ok(app.includes("max_tool_rounds: 256"));
  assert.ok(app.includes('label="默认模型与工具交互轮数上限"'));
  assert.ok(app.includes('patchSection("runtime", "max_tool_rounds", Number(value))'));
  assert.ok(app.includes('value={agent.max_tool_rounds ?? ""}'));
  assert.ok(app.includes("继承全局 ${String(props.document.runtime.max_tool_rounds ?? 256)} 轮"));
  assert.ok(app.includes("当前有效：${agent.max_tool_rounds ?? props.document.runtime.max_tool_rounds ?? 256} 轮"));
  assert.ok(app.includes("max_tool_rounds: value ? Number(value) : undefined"));
  assert.ok(app.includes("作为主 Agent 运行时无固定总时限"));
});

test("预算提醒和耗尽区分告警与错误，明确未完成及不自动重试", () => {
  const logs = [
    { event_type: "run.tool_round_limit_warning", remaining_rounds: 32, message: "梳理待办，不要省略验证" },
    { event_type: "run.tool_round_limit_reached", remaining_rounds: 0, error: "任务未完成", error_code: "agent_tool_round_limit", retryable: false },
  ].map((item, id) => ({ id, run_id: "test", created_at: id, stream: "system", event_type: item.event_type, payload: JSON.stringify({ ...item, request_round: id ? 256 : 225, max_tool_rounds: 256, tool_round_limit_source: "agent" }) }));
  const [warning, reached] = presentRunLogs(logs);
  assert.equal(warning.kind, "warning");
  assert.equal(warning.title, "交互轮数即将耗尽");
  assert.match(warning.detail, /剩余 32 轮/);
  assert.match(warning.body, /不要省略验证/);
  assert.equal(reached.kind, "error");
  assert.match(reached.title, /任务未完成/);
  assert.match(reached.detail, /剩余 0 轮/);
  assert.match(reached.detail, /Agent 配置/);
  assert.match(reached.detail, /停止整次任务自动重试/);
  assert.match(reached.detail, /未完成不代表审核通过/);
});

test("新选模日志显示预算，旧选模日志无预算字段仍可阅读", () => {
  const logs = [true, false].map((withBudget, id) => ({
    id, run_id: "test", created_at: id, stream: "system", event_type: "model.request_started",
    payload: JSON.stringify({ request_round: 225, provider_id: "provider", model: "model", ...(withBudget ? { max_tool_rounds: 256, remaining_rounds: 32 } : {}) }),
  }));
  const [current, legacy] = presentRunLogs(logs);
  assert.match(current.detail, /上限：256 轮；含当前请求剩余 32 轮/);
  assert.match(legacy.detail, /第 225 轮/);
  assert.doesNotMatch(legacy.detail, /undefined|上限/);
});
