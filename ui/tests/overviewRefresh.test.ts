import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createOverviewRefresher } from "../src/overviewRefresh.ts";
import type { OverviewRefreshState } from "../src/overviewRefresh.ts";
import { overviewQuery } from "../src/overviewScope.ts";
import type { OverviewFilter } from "../src/overviewScope.ts";
import { refreshOverviewSelection } from "../src/overviewSelection.ts";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function fixture(load: (path: string) => Promise<string[]>) {
  const results: string[][] = [];
  const states: OverviewRefreshState[] = [];
  const refresher = createOverviewRefresher(load, (result) => results.push(result), (state) => states.push(state));
  return { refresher, results, states };
}

test("普通轮询不显示手动忙碌，手动刷新结束后按钮恢复", async () => {
  const requests = [deferred<string[]>(), deferred<string[]>()];
  const { refresher, states, results } = fixture(() => requests.shift()!.promise);
  const automaticResult = requests[0];
  const automatic = refresher.refresh("/events?page=2");
  assert.equal(states.at(-1)?.busy, false);
  await Promise.resolve();
  automaticResult.resolve(["自动结果"]);
  await automatic;
  const manualResult = requests[0];
  const manual = refresher.refresh("/events?page=2", true);
  assert.equal(states.at(-1)?.busy, true);
  manualResult.resolve(["手动结果"]);
  await manual;
  assert.deepEqual(results, [["自动结果"], ["手动结果"]]);
  assert.deepEqual(states.at(-1), { busy: false, error: "" });
});

test("重复点击和多次轮询共用请求，不让慢响应饥饿或提前解除忙碌", async () => {
  const request = deferred<string[]>();
  let calls = 0;
  const { refresher, states, results } = fixture(() => { calls++; return request.promise; });
  const first = refresher.refresh("/events");
  assert.equal(first, refresher.refresh("/events", true));
  for (let index = 0; index < 5; index++) assert.equal(first, refresher.refresh("/events"));
  assert.equal(first, refresher.refresh("/events", true));
  await Promise.resolve();
  assert.equal(calls, 1);
  assert.equal(states.at(-1)?.busy, true);
  request.resolve(["最新记录"]);
  await first;
  assert.deepEqual(results, [["最新记录"]]);
  assert.equal(states.at(-1)?.busy, false);
  await refresher.refresh("/events", true);
  assert.equal(calls, 2);
});

test("两张表和统计独立，局部失败不会回滚已成功列表或一直锁住顶部按钮", async () => {
  const eventRequest = deferred<string[]>();
  const changeRequest = deferred<string[]>();
  const events = fixture(() => eventRequest.promise);
  const changes = fixture(() => changeRequest.promise);
  const status = fixture(async () => { throw new Error("统计不可用"); });
  const combined = Promise.all([
    events.refresher.refresh("/events", true),
    changes.refresher.refresh("/change-requests", true),
    status.refresher.refresh("/status"),
  ]);
  eventRequest.reject(new Error("事件请求失败"));
  await events.refresher.refresh("/events");
  assert.equal(events.states.at(-1)?.busy, false);
  assert.equal(changes.states.at(-1)?.busy, true);
  changeRequest.resolve(["成功更新的 MR"]);
  await combined;
  assert.deepEqual(changes.results, [["成功更新的 MR"]]);
  assert.deepEqual(events.results, []);
  assert.match(events.states.at(-1)!.error, /事件请求失败/);
  assert.match(status.states.at(-1)!.error, /统计不可用/);
  assert.equal(changes.states.at(-1)?.error, "");
  assert.equal(events.states.at(-1)!.busy || changes.states.at(-1)!.busy, false);
});

test("请求失败保留上次记录，重试成功或成功空列表才替换结果", async () => {
  let fail = false;
  let response = ["已加载记录"];
  const { refresher, results, states } = fixture(async () => {
    if (fail) throw new Error("HTTP 503");
    return response;
  });
  await refresher.refresh("/events", true);
  fail = true;
  await refresher.refresh("/events", true);
  assert.deepEqual(results, [["已加载记录"]]);
  assert.deepEqual(states.at(-1), { busy: false, error: "HTTP 503" });
  fail = false;
  response = [];
  await refresher.refresh("/events");
  assert.deepEqual(results, [["已加载记录"], []]);
  assert.deepEqual(states.at(-1), { busy: false, error: "" });
});

test("非 Error 异常显示可重试提示并恢复按钮", async () => {
  const { refresher, states } = fixture(async () => { throw "异常响应"; });
  await refresher.refresh("/events", true);
  assert.deepEqual(states.at(-1), { busy: false, error: "请求失败，请稍后重试" });
});

test("切换仓库或分页后旧响应与旧错误不能覆盖新范围", async () => {
  for (const failOld of [false, true]) {
    const old = deferred<string[]>();
    const current = deferred<string[]>();
    const { refresher, results, states } = fixture((path) => path.includes("old") ? old.promise : current.promise);
    const previous = refresher.refresh("/events?repository_id=old&page=1", true);
    const next = refresher.refresh("/events?repository_id=new&page=2", true);
    current.resolve(["当前仓库第二页"]);
    await next;
    if (failOld) old.reject(new Error("旧仓库错误"));
    else old.resolve(["旧仓库记录"]);
    await previous;
    assert.deepEqual(results, [["当前仓库第二页"]]);
    assert.deepEqual(states.at(-1), { busy: false, error: "" });
  }
});

test("筛选变化立即失效，迟到旧请求不解除新请求的忙碌状态", async () => {
  const old = deferred<string[]>();
  const current = deferred<string[]>();
  const requests = [old, current];
  const { refresher, states, results } = fixture(() => requests.shift()!.promise);
  const previous = refresher.refresh("/events?page=1", true);
  await Promise.resolve();
  refresher.invalidate();
  old.resolve(["失效结果"]);
  await previous;
  assert.deepEqual(results, []);
  const next = refresher.refresh("/events?page=1", true);
  assert.equal(states.at(-1)?.busy, true);
  current.resolve(["重新请求结果"]);
  await next;
  assert.deepEqual(results, [["重新请求结果"]]);
});

test("旧请求收尾不能提前启用新范围按钮或清除新错误", async () => {
  const old = deferred<string[]>();
  const current = deferred<string[]>();
  const { refresher, states } = fixture((path) => path === "/old" ? old.promise : current.promise);
  const previous = refresher.refresh("/old", true);
  const next = refresher.refresh("/current", true);
  old.reject(new Error("已失效错误"));
  await previous;
  assert.deepEqual(states.at(-1), { busy: true, error: "" });
  assert.equal(refresher.refresh("/current"), next);
  current.reject(new Error("当前范围失败"));
  await next;
  assert.deepEqual(states.at(-1), { busy: false, error: "当前范围失败" });
});

test("刷新保持完整筛选分页查询与跨页选择，不自动选中新记录", async () => {
  const filter: OverviewFilter = {
    repositoryId: "repo", number: "81", status: "", statuses: ["failed"],
    limit: 50, page: 2, sortBy: "occurred_at", sortDirection: "asc",
  };
  const before = structuredClone(filter);
  const query = `/api/events?${overviewQuery(filter, "", true)}`;
  let selected = ["当前页已选", "其他页已选"];
  const paths: string[] = [];
  const refresher = createOverviewRefresher(async (path) => {
    paths.push(path);
    return ["当前页已选", "新事件"];
  }, (result) => { selected = refreshOverviewSelection(selected, result, (item) => item); }, () => undefined);
  await refresher.refresh(query, true);
  assert.deepEqual(filter, before);
  assert.deepEqual(paths, [query]);
  assert.deepEqual(selected, ["当前页已选", "其他页已选"]);
  const parameters = new URL(query, "http://localhost").searchParams;
  assert.equal(parameters.get("repository_id"), "repo");
  assert.equal(parameters.get("number"), "81");
  assert.equal(parameters.get("page"), "2");
  assert.equal(parameters.get("limit"), "50");
  assert.equal(parameters.get("sort_direction"), "asc");
  assert.deepEqual(parameters.getAll("status"), ["failed"]);
});

test("三个刷新入口分工明确，保留轮询且不接入配置加载、扫描或勾选重置", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const overview = app.slice(app.indexOf("function Overview("), app.indexOf("function GlobalEnvironment("));
  for (const [label, state, callback] of [
    ["刷新已扫描 MR / PR", "changeRequestRefresh", "onRefreshChangeRequests"],
    ["刷新最近变化事件", "eventRefresh", "onRefreshEvents"],
  ]) {
    assert.ok(overview.includes(`aria-label="${label}"`));
    assert.ok(overview.includes(`disabled={props.${state}.busy}`));
    assert.ok(overview.includes(`onClick={props.${callback}}`));
    assert.ok(overview.includes(`{props.${state}.busy ? "刷新中…" : "刷新"}`));
  }
  assert.match(app, /aria-label="刷新概览列表"/);
  assert.match(app, /disabled=\{loading \|\| changeRequestRefresh.busy \|\| eventRefresh.busy\}/);
  assert.match(app, /onClick=\{\(\) => \{ void refreshOverviewLists\(\); \}\}/);
  const manual = app.slice(app.indexOf("const refreshOverviewLists ="), app.indexOf("const refreshOverviewData ="));
  assert.match(manual, /Promise.all\(\[refreshChangeRequests\(true\), refreshEvents\(true\)\]\)/);
  assert.doesNotMatch(manual, /refreshOverviewStatus|load\(|scan|setDocument|set.*Filter|setSelected|location/);
  const resources = app.slice(app.indexOf("const changeRequestRefresher ="), app.indexOf("const [executionFilter"));
  assert.equal((resources.match(/cache: "no-store"/g) ?? []).length, 3);
  assert.doesNotMatch(resources, /setDocument|setSavedDocument|setEditing|setSelected|location/);
  assert.match(app, /onRefreshChangeRequests=\{\(\) => \{ void refreshChangeRequests\(true\); \}\}/);
  assert.match(app, /onRefreshEvents=\{\(\) => \{ void refreshEvents\(true\); \}\}/);
  assert.match(app, /window.setInterval\(\(\) => \{ void refreshOverviewData\(\); \}, 3000\)/);
  // 统计未加载或加载失败时，两张表和各自重试入口仍然展示。
  assert.ok(overview.indexOf("<section className=\"section-card\">", overview.indexOf("!overviewStatus")) > overview.indexOf("\n      )}", overview.indexOf("!overviewStatus")));
  assert.match(overview, /MR \/ PR 刷新失败：[\s\S]*当前保留上次列表/);
  assert.match(overview, /最近变化事件刷新失败：[\s\S]*当前保留上次列表/);
});
