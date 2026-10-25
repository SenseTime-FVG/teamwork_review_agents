import assert from "node:assert/strict";
import test from "node:test";
import { runtimeHealth } from "../src/runtimeHealth.ts";
import type { RuntimeStatus } from "../src/types.ts";

const healthy: RuntimeStatus = {
  paused: false,
  running_cycle: false,
  dispatching_events: false,
  config_revision: "test",
  stats: { runs: {}, events: {}, change_requests: {} },
};

test("扫描退出优先于残留扫描中标记，主状态和侧栏都显示异常", () => {
  const result = runtimeHealth({
    ...healthy,
    running_cycle: true,
    background_tasks: { scanner: { state: "stopped", error: "SQLITE_CANTOPEN", failed_at: 123 } },
  });
  assert.equal(result.title, "后台运行异常");
  assert.equal(result.sidebarLabel, "后台异常");
  assert.equal(result.scanLabel, "异常");
  assert.match(result.alerts[0]!.message, /仓库扫描异常：SQLITE_CANTOPEN/);
  assert.equal(result.alerts[0]!.failedAt, 123);
});

test("重试及恢复期间保留故障和重试时间，实际成功后才恢复正常", () => {
  const retry = runtimeHealth({
    ...healthy,
    last_error: "数据库无法打开",
    background_tasks: { scanner: { state: "retrying", error: "数据库无法打开", retry_at: 456 } },
  });
  assert.equal(retry.alerts.length, 1);
  assert.equal(retry.alerts[0]!.retryAt, 456);
  const recovering = runtimeHealth({
    ...healthy,
    running_cycle: true,
    background_tasks: { scanner: { state: "recovering", error: "数据库无法打开" } },
  });
  assert.equal(recovering.hasError, true);
  assert.equal(recovering.alerts[0]!.recovering, true);
  assert.equal(runtimeHealth({ ...healthy, background_tasks: { scanner: { state: "running" } } }).title, "服务运行正常");
});

test("数据库、配置、扫描和独立定时任务错误同时可见", () => {
  const result = runtimeHealth({
    ...healthy,
    config_error: "配置无效",
    last_error: "请求超时",
    last_dispatch_error: "调度失败",
    last_schedule_error: "定时执行失败",
    status_error: "数据库不可用",
  });
  assert.equal(result.alerts.length, 5);
  assert.equal(result.hasError, true);
  assert.match(result.alerts.at(-1)!.message, /后台状态读写失败/);
});

test("旧版状态兼容正常、暂停及扫描中的展示", () => {
  assert.equal(runtimeHealth(healthy).title, "服务运行正常");
  assert.equal(runtimeHealth({ ...healthy, paused: true }).title, "扫描已暂停");
  assert.equal(runtimeHealth({ ...healthy, running_cycle: true }).title, "正在扫描");
});
