import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { presentRunLogs, runMessageRepeatLabel } from "../src/runLogPresentation.ts";
import type { RunLog } from "../src/types.ts";

// 模拟同一次 CI 等待，期限保持不变，只变更查询时间和平台状态条数。
function ciProgress(id: number, statusCount = 0): RunLog {
  return {
    id, run_id: "ci-run", created_at: 100 + id * 15, stream: "system",
    event_type: "ci.wait.progress",
    payload: JSON.stringify({ number: 134, deadline: 1900, status: "pending", check_count: 4, status_count: statusCount }),
  };
}

test("连续七次相同 CI 查询显示总次数，不暗示重复调用工具或额外查询", () => {
  const logs = Array.from({ length: 7 }, (_, index) => ciProgress(index + 1));
  const messages = presentRunLogs(logs);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].repeatCount, 7);
  assert.equal(runMessageRepeatLabel(messages[0]), "相同状态 · 已查询 7 次");
  assert.equal(messages[0].createdAt, logs[0].created_at);
  assert.equal(messages[0].lastCreatedAt, logs[6].created_at);
  assert.equal(messages[0].raw, logs[0].payload);
});

test("CI 状态变化后保持分卡并独立计数，不累加到之前的相同状态", () => {
  const messages = presentRunLogs([
    ...Array.from({ length: 7 }, (_, index) => ciProgress(index + 1)),
    ciProgress(8, 1), ciProgress(9, 1),
  ]);
  assert.equal(messages.length, 2);
  assert.deepEqual(messages.map(runMessageRepeatLabel), [
    "相同状态 · 已查询 7 次", "相同状态 · 已查询 2 次",
  ]);
  assert.deepEqual(messages.map((message) => JSON.parse(message.raw).deadline), [1900, 1900]);
});

test("单条 CI 查询不显示重复徽标，其他重复日志保持原文案", () => {
  const [single] = presentRunLogs([ciProgress(1)]);
  assert.equal(runMessageRepeatLabel(single), "");
  const messages = presentRunLogs([1, 2].map((id) => ({
    ...ciProgress(id), event_type: "workspace.git.progress",
    payload: JSON.stringify({ operation: "克隆基础仓库" }),
  })));
  assert.equal(messages.length, 1);
  assert.equal(runMessageRepeatLabel(messages[0]), "重复 2 次");
});

test("消息卡片接入按事件区分的计数文案，并保留单条不显示的条件", () => {
  const component = readFileSync(new URL("../src/RunMessageFeed.tsx", import.meta.url), "utf8");
  assert.match(component, /message\.repeatCount > 1 && <em>\{runMessageRepeatLabel\(message\)\}<\/em>/);
  assert.doesNotMatch(component, /<em>重复 \{message\.repeatCount\} 次<\/em>/);
});
