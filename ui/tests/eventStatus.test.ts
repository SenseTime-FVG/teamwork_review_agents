import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { EVENT_STATUS_OPTIONS, eventStatusPresentation } from "../src/eventStatusPresentation.ts";
import type { EventRecord } from "../src/types.ts";

// 只提供本展示函数读取的字段，不构造与状态无关的网络响应数据。
const record = (values: Partial<EventRecord>): EventRecord => ({ status: "processing", trigger_count: 0, ...values } as EventRecord);

test("处理中与是否开始本地 CI 无关，筛选保持同名", () => {
  for (const preflight_status of [null, "running", "success", "failure"] as const) {
    const result = eventStatusPresentation(record({ preflight_status }));
    assert.equal(result.label, "处理中");
    assert.equal(result.visualStatus, "processing");
  }
  assert.equal(EVENT_STATUS_OPTIONS.find(item => item.value === "processing")?.label, "处理中");
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(app, /import \{ EVENT_STATUS_OPTIONS, eventStatusPresentation, unmatchedReasonLabel \} from "\.\/eventStatusPresentation"/);
  assert.doesNotMatch(app, /规则匹配中|本地 CI 中/);
});

test("CI 未通过等终态仍展示真实门禁结论", () => {
  const failure = eventStatusPresentation(record({ status: "completed", preflight_status: "failure", preflight_failed_step: "tests" }));
  assert.equal(failure.label, "本地 CI 未通过");
  assert.equal(failure.visualStatus, "failed");
  assert.equal(failure.details, "失败步骤：tests");
  assert.equal(eventStatusPresentation(record({ status: "completed", preflight_status: "timed_out" })).label, "本地 CI 超时");
  assert.equal(eventStatusPresentation(record({ status: "failed", preflight_status: "error" })).label, "本地 CI 异常");
  assert.equal(eventStatusPresentation(record({ status: "completed", preflight_status: "success", trigger_count: 1 })).label, "已处理");
  assert.equal(eventStatusPresentation(record({ status: "unmatched" })).label, "未触发");
});

test("状态回写失败和旧记录原因保持可见", () => {
  const failure = eventStatusPresentation(record({ error: "GitHub 状态回写失败" }));
  assert.equal(failure.label, "状态回写失败");
  assert.equal(failure.visualStatus, "failed");
  assert.equal(failure.details, "GitHub 状态回写失败");
  const unmatched = eventStatusPresentation(record({ status: "unmatched", unmatched_reason: "scan_deduplicated" }));
  assert.match(unmatched.details ?? "", /已被更新事件替代/);
});
