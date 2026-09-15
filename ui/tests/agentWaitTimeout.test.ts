// 验证超时配置与状态文案，避免把远端等待、本地执行和子任务总时限混为一谈。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");

test("主子 Agent 时限区分，远端 CI 默认三十分钟且可由仓库覆盖", () => {
  assert.ok(app.includes("作为子 Agent 的总超时（秒）"));
  assert.ok(app.includes("作为主 Agent 运行时无固定总时限"));
  assert.ok(app.includes("remote_ci_wait_timeout_seconds ?? 1800"));
  assert.ok(app.includes("远端 CI 等待超时（分钟，可选）"));
  assert.ok(app.includes("CI 总超时（秒）"));
});

test("CI 到期显示待处理和固定截止时间，不承诺自动恢复或合并", () => {
  assert.ok(app.includes("等待超时，待处理"));
  assert.ok(app.includes("轮询不重置期限"));
  assert.ok(app.includes("不会自动从头重跑或合并"));
});
