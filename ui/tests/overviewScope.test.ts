import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { overviewQuery, overviewStatusPath } from "../src/overviewScope.ts";
import type { OverviewFilter } from "../src/overviewScope.ts";

const filter: OverviewFilter = {
  number: "151",
  status: "",
  statuses: ["processing", "failed"],
  limit: 20,
  page: 2,
  sortBy: "number",
  sortDirection: "asc",
};

test("仓库范围统一进入统计和两个列表请求，ID 按原值编码", () => {
  const repositoryId = "仓库 A&B/%";
  const statusUrl = new URL(overviewStatusPath(repositoryId), "http://localhost");
  assert.equal(statusUrl.searchParams.get("repository_id"), repositoryId);
  for (const numbered of [false, true]) {
    const query = new URLSearchParams(overviewQuery(filter, repositoryId, numbered));
    assert.equal(query.get("repository_id"), repositoryId);
    assert.equal(query.get("number"), numbered ? "151" : null);
    assert.deepEqual(query.getAll("status"), ["processing", "failed"]);
    assert.equal(query.get("page"), "2");
    assert.equal(query.get("limit"), "20");
    assert.equal(query.get("sort_by"), "number");
    assert.equal(query.get("sort_direction"), "asc");
  }
});

test("全部仓库恢复无范围请求且保留列表自己的筛选", () => {
  assert.equal(overviewStatusPath(""), "/api/status");
  const query = new URLSearchParams(overviewQuery({ ...filter, limit: null }, "", true));
  assert.equal(query.has("repository_id"), false);
  assert.equal(query.has("limit"), false);
  assert.equal(query.get("all_records"), "true");
  assert.equal(query.get("number"), "151");
  assert.deepEqual(query.getAll("status"), filter.statuses);
  for (const number of ["0", "-1", "1.2", "abc", ""]) {
    assert.equal(new URLSearchParams(overviewQuery({ ...filter, number }, "repo", true)).has("number"), false);
  }
});

test("两个概览列表分别携带自己的默认排序", () => {
  const changeRequestQuery = new URLSearchParams(overviewQuery({
    ...filter,
    sortBy: "updated_at",
    sortDirection: "desc",
  }, "", false));
  assert.equal(changeRequestQuery.get("sort_by"), "updated_at");
  assert.equal(changeRequestQuery.get("sort_direction"), "desc");

  const eventQuery = new URLSearchParams(overviewQuery({
    ...filter,
    sortBy: "occurred_at",
    sortDirection: "desc",
  }, "", true));
  assert.equal(eventQuery.get("sort_by"), "occurred_at");
  assert.equal(eventQuery.get("sort_direction"), "desc");

  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(app, /DEFAULT_CHANGE_REQUEST_FILTER[\s\S]*sortBy: "updated_at"[\s\S]*sortDirection: "desc"/);
  assert.match(app, /DEFAULT_EVENT_FILTER[\s\S]*sortBy: "occurred_at"[\s\S]*sortDirection: "desc"/);
  assert.match(app, /\{ value: "latest_event_at", label: "最新平台事件时间" \}/);
  assert.match(app, /\{ value: "occurred_at", label: "事件时间" \}/);
  assert.match(app, /props\.filter\.sortBy === "number"/);
});

test("仓库选择接线保持单一范围，清理跨仓库状态并隔离旧请求", () => {
  // 现有轻量前端测试通过源码接线检查防止重新引入重复仓库选择器。
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const controls = app.slice(app.indexOf("function OverviewListControls"), app.indexOf("function OverviewPagination"));
  assert.doesNotMatch(controls, /仓库|repositoryId/);
  assert.match(app, /ariaLabel="运行概览仓库"/);
  assert.match(app, /overviewQuery\(eventFilter, overviewRepositoryId, true\)/);
  assert.match(app, /overviewQuery\(changeRequestFilter, overviewRepositoryId\)/);
  assert.match(app, /overviewStatusPath\(overviewRepositoryId\)/);
  const change = app.slice(app.indexOf("const changeOverviewRepository ="), app.indexOf("const load = useCallback"));
  assert.match(change, /overviewRequestSequence.current \+= 1/);
  assert.match(change, /setOverviewStatus\(null\)/);
  assert.match(change, /setChangeRequestFilter\(\(current\) => \(\{ \.\.\.current, page: 1 \}\)\)/);
  assert.match(change, /setEventFilter\(\(current\) => \(\{ \.\.\.current, page: 1 \}\)\)/);
  assert.match(change, /setSelectedSnapshotKeys\(\[\]\)/);
  assert.match(change, /setSelectedEventIds\(\[\]\)/);
  assert.match(change, /setOverviewConfirmation\(null\)/);
  assert.match(app, /key=\{overviewRepositoryId\}/);
  assert.match(app, /requestSequence !== overviewRequestSequence.current\) return/);
  assert.match(app, /后台调度器 · 全局/);
  assert.match(app, /最近成功扫描/);
});
