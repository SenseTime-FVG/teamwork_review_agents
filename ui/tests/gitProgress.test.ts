import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { gitBytesText, gitProgressText, presentRunLogs } from "../src/runLogPresentation.ts";

test("真实 Git 阶段显示对象、字节和速度，不将阶段百分比当成总进度", () => {
  const text = gitProgressText({ timeout_kind: "idle", idle_seconds: 7, progress: {
    stage: "Receiving objects", label: "接收对象", percent: 20, current: 2, total: 10,
    received_bytes: 1048576, bytes_per_second: 2048,
  } });
  assert.match(text, /接收对象.*当前阶段 20%.*2 \/ 10.*1\.0 MiB.*2\.0 KiB\/s.*无有效进展 7 秒/);
  assert.equal(gitBytesText(0), "0 B");
});

test("无百分比与旧记录不虚构进度，Agent 日志使用无进展超时语义", () => {
  assert.equal(gitProgressText({ elapsed_seconds: 2000 }), "");
  const unknown = gitProgressText({ timeout_kind: "idle", idle_seconds: 61 });
  assert.match(unknown, /等待 Git 报告可量化进度.*61 秒/);
  assert.doesNotMatch(unknown, /%/);
  const [message] = presentRunLogs([{
    id: 1, run_id: "test", created_at: 1, stream: "system", event_type: "workspace.git.progress",
    payload: JSON.stringify({ operation: "克隆基础仓库", timeout_kind: "idle", timeout_seconds: 1800, elapsed_seconds: 3600, idle_seconds: 5 }),
  }]);
  assert.match(message.detail, /总耗时：3600 秒/);
  assert.match(message.detail, /无进展超时：1800 秒/);
  assert.match(message.detail, /无有效进展 5 秒/);
});

test("详情区分锁等待与旧超时，提供阶段进度和停滞提示", () => {
  const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /<progress aria-label=/);
  assert.match(source, /最近有效进展/);
  assert.match(source, /总超时（旧记录）/);
  assert.match(source, /等待超时/);
  assert.match(source, /可能停滞/);
  assert.match(source, /基础仓库初始化无进展超时（秒）/);
  const style = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(style, /progress::-webkit-progress-value \{ background: var\(--accent\)/);
});
