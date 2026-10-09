// 版本探测等待与终态展示回归，不连接真实服务。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { codexVersionProbeWaitText, presentRunLogs } from "../src/runLogPresentation.ts";
import type { RunLog } from "../src/types.ts";

function retryLog(): RunLog {
  return { id: 1, run_id: "test", created_at: 100, stream: "system", event_type: "runtime.codex_version_retry",
    payload: JSON.stringify({ attempt: 3, next_attempt: 4, max_attempts: 6, delay_seconds: 40, retry_at: 140, reason: "退出码 1" }) };
}

test("探测失败是等待告警，恢复后继续当前请求而非重跑", () => {
  const retry = retryLog();
  const recovered = { ...retry, id: 2, event_type: "runtime.codex_version_recovered", payload: JSON.stringify({ attempt: 4, max_attempts: 6, version: "0.159.2" }) };
  const [waiting, resumed] = presentRunLogs([retry, recovered]);
  assert.equal(waiting.kind, "warning");
  assert.match(waiting.body, /3 \/ 6.*40 秒.*第 4 次/);
  assert.match(waiting.body, /不重跑 Agent/);
  assert.equal(waiting.detail, "退出码 1");
  assert.equal(resumed.kind, "system");
  assert.match(resumed.body, /0.159.2.*继续当前模型请求/);
});

test("只有最新等待显示动态秒数，到点显示探测中，后续日志及终态清除倒计时", () => {
  const retry = retryLog();
  assert.match(codexVersionProbeWaitText([retry], true, 100), /40 秒后.*4 \/ 6.*可取消/);
  assert.match(codexVersionProbeWaitText([retry], true, 139.5), /1 秒后/);
  assert.match(codexVersionProbeWaitText([retry], true, 141), /正在进行第 4 \/ 6 次/);
  assert.equal(codexVersionProbeWaitText([retry], false, 110), "");
  for (const event_type of ["runtime.codex_version_recovered", "run.runtime_unavailable", "run.cancelled", "thread.started"]) {
    assert.equal(codexVersionProbeWaitText([retry, { ...retry, id: 2, event_type }], true, 110), "");
  }
  assert.equal(codexVersionProbeWaitText([{ ...retry, payload: "{}" }], true, 110), "");
});

test("消息页用本地计时器刷新等待，停止运行后不继续显示", () => {
  const feed = readFileSync(new URL("../src/RunMessageFeed.tsx", import.meta.url), "utf8");
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(feed, /window\.setInterval\(\(\) => setNow\(Date\.now\(\) \/ 1000\), 1000\)/);
  assert.match(feed, /window\.clearInterval\(timer\)/);
  assert.match(feed, /className="alert run-version-wait-note"/);
  assert.match(app, /running=\{detail\.status === "running"\}/);
});
