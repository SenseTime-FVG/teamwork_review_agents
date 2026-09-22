import assert from "node:assert/strict";
import test from "node:test";
import { activitySourceLabel, manualTriggerEvent, platformActivityStatus } from "../src/changeRequestActivity.ts";
import type { ChangeRequestRecord } from "../src/types.ts";

test("系统检测候选不依赖平台事件，明确空值不能回退旧缓存", () => {
  const record = {
    latest_event: { event_type: "change_request.merged" },
    manual_event: { event_type: "change_request.commits_changed", source: "system" },
  } as ChangeRequestRecord;
  assert.equal(manualTriggerEvent(record)?.source, "system");
  assert.equal(activitySourceLabel("system"), "系统检测");
  assert.equal(manualTriggerEvent({ ...record, manual_event: null }), null);
});

test("区分等待、无事件和读取失败，失败时不使用旧接口的平台缓存", () => {
  const record = {} as ChangeRequestRecord;
  assert.equal(platformActivityStatus(record), "等待扫描获取");
  assert.equal(platformActivityStatus({ ...record, latest_event_checked: true }), "暂无可识别平台事件");
  const failed = { ...record, latest_event_error: "接口失败", latest_event: { event_type: "change_request.merged" } } as ChangeRequestRecord;
  assert.equal(platformActivityStatus(failed), "平台活动读取失败");
  assert.equal(manualTriggerEvent(failed), null);
});
