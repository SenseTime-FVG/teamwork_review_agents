import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { overviewQuery, overviewRepositoryOptions, overviewStatusPath, toggleOverviewSort } from "../src/overviewScope.ts";
import type { OverviewFilter, OverviewSortField } from "../src/overviewScope.ts";

const filter: OverviewFilter = {
  repositoryId: "",
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
});

test("点击表头切换字段默认降序，重复点击反转方向并回到第一页", () => {
  const fields: OverviewSortField[] = ["number", "updated_at", "scanned_at", "latest_event_at", "occurred_at"];
  for (const field of fields) {
    const original: OverviewFilter = {
      ...filter,
      sortBy: field === "number" ? "updated_at" : "number",
      sortDirection: "desc",
    };
    const descending = toggleOverviewSort(original, field);
    assert.deepEqual(descending, { ...original, page: 1, sortBy: field, sortDirection: "desc" });
    const ascending = toggleOverviewSort(descending, field);
    assert.deepEqual(ascending, { ...descending, sortDirection: "asc" });
    assert.deepEqual(toggleOverviewSort(ascending, field), descending);
    assert.equal(original.page, 2);
    assert.equal(original.sortDirection, "desc");
  }
});

test("排序入口绑定对应表头与列表回调，移除额外排序下拉框", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const controls = app.slice(app.indexOf("function OverviewListControls"), app.indexOf("function OverviewPagination"));
  assert.doesNotMatch(controls, /label="排序"|label="顺序"|sortOptions/);
  const headers = [...app.matchAll(/<OverviewSortHeader label="([^"]+)" field="([^"]+)" filter=\{props\.(\w+)\} onChange=\{props\.(\w+)\}/g)]
    .map((match) => match.slice(1));
  assert.deepEqual(headers, [
    ["MR / PR", "number", "changeRequestFilter", "onChangeRequestFilterChange"],
    ["远端更新", "updated_at", "changeRequestFilter", "onChangeRequestFilterChange"],
    ["最近扫描", "scanned_at", "changeRequestFilter", "onChangeRequestFilterChange"],
    ["最新平台事件", "latest_event_at", "changeRequestFilter", "onChangeRequestFilterChange"],
    ["编号", "number", "eventFilter", "onEventFilterChange"],
    ["时间", "occurred_at", "eventFilter", "onEventFilterChange"],
  ]);
  const header = app.slice(app.indexOf("function OverviewSortHeader"), app.indexOf("function OverviewListControls"));
  assert.match(header, /<button\s+type="button"/);
  assert.match(header, /aria-sort=\{active \? \(props\.filter\.sortDirection === "asc" \? "ascending" : "descending"\) : undefined\}/);
  assert.match(header, /onClick=\{\(\) => props\.onChange\(nextFilter\)\}/);
  assert.match(app, /function changeOverviewChangeRequestFilter[\s\S]*?cancelChangeRequestSelection\(\);[\s\S]*?setChangeRequestFilter\(\{ \.\.\.filter, page: 1 \}\)/);
  assert.match(app, /function changeOverviewEventFilter[\s\S]*?cancelEventSelection\(\);[\s\S]*?setEventFilter\(\{ \.\.\.filter, page: 1 \}\)/);
});

test("顶部具体仓库优先，全部模式允许两个列表各自筛选", () => {
  const snapshots = { ...filter, repositoryId: "a" };
  const events = { ...filter, repositoryId: "b" };
  assert.equal(new URLSearchParams(overviewQuery(snapshots, "")).get("repository_id"), "a");
  assert.equal(new URLSearchParams(overviewQuery(events, "", true)).get("repository_id"), "b");
  assert.equal(overviewStatusPath(""), "/api/status");
  for (const local of [snapshots, events]) {
    const query = new URLSearchParams(overviewQuery(local, "top"));
    assert.equal(query.get("repository_id"), "top");
    assert.equal(query.get("sort_direction"), "asc");
  }
});

test("只有重名的仓库追加 ID，无名称和首尾空白也正确处理", () => {
  assert.deepEqual(overviewRepositoryOptions([
    { id: "repo-a", display_name: "Box-Agent" },
    { id: "repo-b", display_name: " 重名 " },
    { id: "repo-c", display_name: "重名" },
    { id: "legacy", display_name: "" },
    { id: "unique" },
    { id: "alias", display_name: "legacy" },
  ]), [
    { value: "", label: "全部仓库" },
    { value: "repo-a", label: "Box-Agent" },
    { value: "repo-b", label: "重名（repo-b）" },
    { value: "repo-c", label: "重名（repo-c）" },
    { value: "legacy", label: "legacy（legacy）" },
    { value: "unique", label: "unique" },
    { value: "alias", label: "legacy（alias）" },
  ]);
  assert.deepEqual(overviewRepositoryOptions([]), [{ value: "", label: "全部仓库" }]);
});

test("仓库选择区分整体与局部范围，清理跨仓库状态并隔离旧请求", () => {
  // 验证局部下拉只在全部模式展示，同时保留既有排序接线和请求隔离。
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const controls = app.slice(app.indexOf("function OverviewListControls"), app.indexOf("function OverviewPagination"));
  assert.match(controls, /props.repositoryOptions &&/);
  assert.match(controls, /value=\{props.filter.repositoryId\}/);
  assert.equal((app.match(/repositoryOptions=\{props.repositoryId \? undefined : props.repositoryOptions\}/g) ?? []).length, 2);
  assert.match(app, /ariaLabel="运行概览仓库"/);
  assert.match(app, /overviewQuery\(eventFilter, overviewRepositoryId, true\)/);
  assert.match(app, /overviewQuery\(changeRequestFilter, overviewRepositoryId\)/);
  assert.match(app, /overviewStatusPath\(overviewRepositoryId\)/);
  const change = app.slice(app.indexOf("const changeOverviewRepository ="), app.indexOf("const load = useCallback"));
  assert.match(change, /overviewRequestSequence.current \+= 1/);
  assert.match(change, /setOverviewStatus\(null\)/);
  assert.match(change, /setChangeRequestFilter\(\(current\) => \(\{ \.\.\.current, repositoryId: "", page: 1 \}\)\)/);
  assert.match(change, /setEventFilter\(\(current\) => \(\{ \.\.\.current, repositoryId: "", page: 1 \}\)\)/);
  assert.match(change, /setSelectedSnapshotKeys\(\[\]\)/);
  assert.match(change, /setSelectedEventIds\(\[\]\)/);
  assert.match(change, /setOverviewConfirmation\(null\)/);
  assert.match(app, /key=\{overviewRepositoryId\}/);
  assert.match(app, /requestSequence !== overviewRequestSequence.current\) return/);
  assert.match(app, /后台调度器 · 全局/);
  assert.match(app, /最近成功扫描/);
});
