import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { DEFAULT_WORKSPACE_CLEANUP, cleanupResultLabel, workspaceCleanupSummary } from "../src/workspaceCleanup.ts";

// 验证纯配置逻辑及组件接线，不连接真实后台、不执行清理。
const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const panel = app.slice(app.indexOf("function WorkspaceCleanupPanel("), app.indexOf("function ConfigHistory("));
const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("清理默认每天六点，本地时间不提供时区字段", () => {
  assert.equal(DEFAULT_WORKSPACE_CLEANUP.enabled, true);
  assert.equal(workspaceCleanupSummary(DEFAULT_WORKSPACE_CLEANUP), "每天 06:00");
  assert.equal("timezone" in DEFAULT_WORKSPACE_CLEANUP, false);
  assert.doesNotMatch(panel, /label="[^"]*时区/);
  assert.match(panel, /worktree_retention_days \?\? 7/);
});

test("支持固定小时或天间隔及每小时、每天、每周定点计划", () => {
  assert.equal(workspaceCleanupSummary({ ...DEFAULT_WORKSPACE_CLEANUP, kind: "interval", interval_value: 3, interval_unit: "hours" }), "每 3 小时");
  assert.equal(workspaceCleanupSummary({ ...DEFAULT_WORKSPACE_CLEANUP, kind: "interval", interval_value: 2 }), "每 2 天");
  assert.equal(workspaceCleanupSummary({ ...DEFAULT_WORKSPACE_CLEANUP, kind: "hourly", minute: 15 }), "每小时第 15 分");
  assert.equal(workspaceCleanupSummary({ ...DEFAULT_WORKSPACE_CLEANUP, kind: "weekly", weekday: 6, hour: 8, minute: 30 }), "每周日 08:30");
  assert.equal(workspaceCleanupSummary({ ...DEFAULT_WORKSPACE_CLEANUP, enabled: false }), "定时清理已停用");
});

test("卡片位于配置历史上方，唯一保留期入口沿用编辑草稿", () => {
  assert.ok(app.indexOf("<WorkspaceCleanupPanel ") < app.indexOf("<ConfigHistory />"));
  assert.equal([...app.matchAll(/label="工作区保留期（天）"/g)].length, 1);
  assert.match(panel, /fieldset[^>]+disabled=\{!props.editing\}/);
  assert.match(panel, /props.onChange\(\{ \.\.\.props.document, runtime:/);
  assert.match(panel, /retention < savedRetention/);
  assert.match(panel, /下次清理可能删除更多已有工作区/);
  assert.match(panel, /配置尚未保存；下方时间与结果对应已保存计划/);
});

test("状态只读查询，直接展示服务端时间且保留失败详情", () => {
  assert.match(panel, /api<WorkspaceCleanupStatus>\("\/api\/runtime\/workspace-cleanup"\)/);
  assert.doesNotMatch(panel, /method:|new Date\(|timeText\(/);
  assert.match(panel, /status.next_run_text/);
  assert.match(panel, /last\?\.started_text/);
  assert.match(panel, /status\?\.scheduler_error/);
  assert.match(panel, /item.reason/);
  assert.match(panel, /释放空间（估算）/);
  assert.equal(cleanupResultLabel("partial"), "部分清理失败");
  assert.equal(cleanupResultLabel("interrupted"), "已中断");
  assert.match(styles, /\.workspace-cleanup-card input\s*\{[^}]*color-scheme: dark/);
});
