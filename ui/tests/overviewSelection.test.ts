import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { overviewSelectionRange, toggleOverviewSelection, toggleOverviewRecords, refreshOverviewSelection, overviewSelectionScopeChanged } from "../src/overviewSelection.ts";
import type { OverviewSelectionAnchor, OverviewSelectionRow } from "../src/overviewSelection.ts";
import type { OverviewFilter } from "../src/overviewScope.ts";

const rows: OverviewSelectionRow[] = ["a", "b", "c", "d", "e"].map((key) => ({ key, selectable: true }));

function anchorAt(key: string): OverviewSelectionAnchor {
  // 用稳定 ID 与当前可见顺序建立起点，不依赖索引缓存。
  return { key, visibleKeys: rows.map((row) => row.key) };
}

test("普通点击只切换单项并更新起点，Shift 无起点时退化为单项", () => {
  for (const shift of [false, true]) {
    const range = overviewSelectionRange(rows, "b", null, shift)!;
    assert.deepEqual(range.keys, ["b"]);
    assert.deepEqual(range.anchor, anchorAt("b"));
    assert.deepEqual(toggleOverviewSelection(["e"], "b", range.keys), ["e", "b"]);
    assert.deepEqual(toggleOverviewSelection(["e", "b"], "b", range.keys), ["e"]);
  }
  assert.deepEqual(overviewSelectionRange(rows, "c", anchorAt("a"), false)?.anchor, anchorAt("c"));
});

test("Shift 正反方向均包含首尾且保留区间外选择", () => {
  for (const [anchor, target] of [["b", "d"], ["d", "b"]]) {
    const range = overviewSelectionRange(rows, target, anchorAt(anchor), true)!;
    assert.deepEqual(range.keys, ["b", "c", "d"]);
    assert.deepEqual(range.anchor, anchorAt(anchor));
    assert.deepEqual(new Set(toggleOverviewSelection(["e", anchor], target, range.keys)), new Set(["b", "c", "d", "e"]));
  }
});

test("已勾选目标会取消整段，混合状态按目标统一而不是逐项反转", () => {
  const range = overviewSelectionRange(rows, "d", anchorAt("b"), true)!;
  assert.deepEqual(toggleOverviewSelection(["a", "b", "d", "e"], "d", range.keys), ["a", "e"]);
  assert.deepEqual(toggleOverviewSelection(["a", "b", "e"], "d", range.keys), ["a", "b", "e", "c", "d"]);
  assert.equal(new Set(toggleOverviewSelection(["a", "b"], "d", range.keys)).size, 4);
});

test("重复 Shift 扩展保留起点，Shift 点击起点本身只切换单项", () => {
  const first = overviewSelectionRange(rows, "c", anchorAt("a"), true)!;
  const next = overviewSelectionRange(rows, "e", first.anchor, true)!;
  assert.deepEqual(next.keys, ["a", "b", "c", "d", "e"]);
  assert.deepEqual(overviewSelectionRange(rows, "a", next.anchor, true)?.keys, ["a"]);
});

test("范围跳过禁用记录，点击禁用或不存在的目标不修改起点", () => {
  const disabled = rows.map((row) => ({ ...row, selectable: row.key !== "c" }));
  assert.deepEqual(overviewSelectionRange(disabled, "e", anchorAt("a"), true)?.keys, ["a", "b", "d", "e"]);
  assert.equal(overviewSelectionRange(disabled, "c", anchorAt("a"), true), null);
  assert.equal(overviewSelectionRange(disabled, "missing", anchorAt("a"), false), null);
  assert.equal(overviewSelectionRange([], "a", null, true), null);
});

test("相同顺序的自动刷新保留起点，排序、增删和翻页后不套用旧范围", () => {
  assert.deepEqual(overviewSelectionRange(rows.map((row) => ({ ...row })), "d", anchorAt("a"), true)?.keys, ["a", "b", "c", "d"]);
  const changedLists = [
    [...rows].reverse(),
    rows.slice(1),
    [...rows, { key: "f", selectable: true }],
    rows.filter((row) => row.key !== "c"),
    [{ key: "d", selectable: true }, { key: "page-2", selectable: true }],
  ];
  for (const changed of changedLists) {
    const range = overviewSelectionRange(changed, "d", anchorAt("a"), true)!;
    assert.deepEqual(range.keys, ["d"]);
    assert.equal(range.anchor.key, "d");
    assert.deepEqual(range.anchor.visibleKeys, changed.map((row) => row.key));
  }
  assert.deepEqual(overviewSelectionRange(rows, "d", { ...anchorAt("a"), key: "removed" }, true)?.keys, ["d"]);
});

test("两张表的起点互不影响，筛选和取消入口清除对应起点", () => {
  const requests = overviewSelectionRange(rows, "a", null, false)!;
  const events = overviewSelectionRange(rows, "d", null, false)!;
  assert.deepEqual(overviewSelectionRange(rows, "c", requests.anchor, true)?.keys, ["a", "b", "c"]);
  assert.deepEqual(overviewSelectionRange(rows, "e", events.anchor, true)?.keys, ["d", "e"]);
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(app, /const changeRequestSelectionAnchor = useRef<OverviewSelectionAnchor \| null>\(null\)/);
  assert.match(app, /const eventSelectionAnchor = useRef<OverviewSelectionAnchor \| null>\(null\)/);
  assert.match(app, /function cancelChangeRequestSelection\(\) \{\s*changeRequestSelectionAnchor.current = null/);
  assert.match(app, /function cancelEventSelection\(\) \{\s*eventSelectionAnchor.current = null/);
  for (const action of ["changeOverviewChangeRequestFilter"]) {
    const handler = app.slice(app.indexOf(`function ${action}(`));
    assert.match(handler.slice(0, handler.indexOf("\n  }")), /cancelChangeRequestSelection\(\)/);
  }
  for (const action of ["changeOverviewEventFilter"]) {
    const handler = app.slice(app.indexOf(`function ${action}(`));
    assert.match(handler.slice(0, handler.indexOf("\n  }")), /cancelEventSelection\(\)/);
  }
});

test("扩大点击区域只在选择模式启用，忙碌守卫与控件键盘冒泡保护保留", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const overview = app.slice(app.indexOf("function Overview("), app.indexOf("function GlobalEnvironment("));
  const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.equal((overview.match(/className="overview-selection-column overview-selection-target"/g) ?? []).length, 2);
  assert.match(overview, /onClick=\{props.selectionMode \? \(event\) =>/);
  assert.match(overview, /onClick=\{props.eventSelectionMode \? \(clickEvent\) =>/);
  assert.match(overview, /props.onToggleSelection\(item.snapshot_key, event.shiftKey\)/);
  assert.match(overview, /props.onToggleEventSelection\(event.event_id, clickEvent.shiftKey\)/);
  assert.match(overview, /event.target !== event.currentTarget/);
  assert.match(overview, /keyboardEvent.target !== keyboardEvent.currentTarget/);
  assert.match(overview, /<a className="change-request-link" href=\{item.web_url\}/);
  assert.match(app, /if \(!changeRequestSelectionMode \|\| triggeringKeys.length > 0\) return/);
  assert.match(app, /if \(!eventSelectionMode \|\| replayingEventIds.length > 0\) return/);
  assert.match(css, /\.overview-selection-target \{[^}]*user-select: none/);
  assert.match(css, /\.overview-selection-target\[aria-disabled="true"\]/);
});

test("跨页选择保存完整记录，回到原页可取消且重复点击不产生重复目标", () => {
  const firstPage = [{ id: "a", title: "第一页" }, { id: "b", title: "第一页第二项" }];
  const secondPage = [{ id: "c", title: "第二页" }, { id: "d", title: "第二页第二项" }];
  const keyOf = (item: typeof firstPage[number]) => item.id;
  let selected = toggleOverviewRecords([], firstPage, keyOf, "a", ["a"]);
  selected = toggleOverviewRecords(selected, secondPage, keyOf, "d", ["c", "d"]);
  assert.deepEqual(selected, [firstPage[0], secondPage[0], secondPage[1]]);
  selected = toggleOverviewRecords(selected, firstPage, keyOf, "b", ["a", "b"]);
  assert.deepEqual(selected.map(keyOf), ["a", "c", "d", "b"]);
  assert.equal(new Set(selected.map(keyOf)).size, selected.length);
  selected = toggleOverviewRecords(selected, firstPage, keyOf, "a", ["a"]);
  assert.deepEqual(selected.map(keyOf), ["c", "d", "b"]);
});

test("当前页刷新只更新已选同 ID 记录，不清理其他页、不自动选择新增记录", () => {
  const selected = [{ id: "a", title: "旧标题" }, { id: "c", title: "其他页" }];
  const keyOf = (item: typeof selected[number]) => item.id;
  const updated = { id: "a", title: "新标题" };
  const refreshed = refreshOverviewSelection(selected, [updated, { id: "b", title: "未选择" }], keyOf);
  assert.deepEqual(refreshed, [updated, selected[1]]);
  assert.equal(refreshed[1], selected[1]);
  assert.equal(refreshOverviewSelection(selected, [], keyOf), selected);
  assert.equal(refreshOverviewSelection(selected, [selected[0]], keyOf), selected);
  assert.deepEqual(refreshOverviewSelection([], [updated], keyOf), []);
});

test("当前页 Shift 取消区间不影响其他页已选记录", () => {
  const selected = [{ key: "previous" }, { key: "a" }, { key: "b" }];
  const visible = rows.map((row) => ({ key: row.key }));
  const range = overviewSelectionRange(rows, "b", anchorAt("a"), true)!;
  assert.deepEqual(toggleOverviewRecords(selected, visible, (item) => item.key, "b", range.keys), [{ key: "previous" }]);
});

const filter: OverviewFilter = { repositoryId: "", number: "", status: "", statuses: [], limit: 10, page: 1, sortBy: "number", sortDirection: "asc" };

test("翻页、排序和条数不改变选择范围，仓库、编号及有效状态变化才清空", () => {
  for (const change of [{ page: 2 }, { sortBy: "updated_at" as const }, { sortDirection: "desc" as const }, { limit: 50 }, { limit: null }]) {
    assert.equal(overviewSelectionScopeChanged(filter, { ...filter, ...change }), false);
  }
  for (const change of [{ repositoryId: "repo" }, { number: "123" }, { status: "failed" }, { statuses: ["failed"] }]) {
    assert.equal(overviewSelectionScopeChanged(filter, { ...filter, ...change }), true);
  }
  assert.equal(overviewSelectionScopeChanged({ ...filter, status: "failed" }, { ...filter, statuses: ["failed"] }), false);
  assert.equal(overviewSelectionScopeChanged({ ...filter, statuses: ["failed", "pending"] }, { ...filter, statuses: ["pending", "failed", "failed"] }), false);
});

test("翻页仅清起点和隔离迟到请求，按钮和提交按完整跨页集合接线", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  for (const [action, anchor] of [["changeOverviewChangeRequestPage", "changeRequestSelectionAnchor"], ["changeOverviewEventPage", "eventSelectionAnchor"]]) {
    const handler = app.slice(app.indexOf(`function ${action}(`));
    const body = handler.slice(0, handler.indexOf("\n  }"));
    assert.match(body, /overviewRequestSequence.current \+= 1/);
    assert.ok(body.includes(`${anchor}.current = null`));
    assert.doesNotMatch(body, /cancelChangeRequestSelection|cancelEventSelection|setSelected/);
  }
  assert.match(app, /if \(overviewSelectionScopeChanged\(changeRequestFilter, filter\)\) cancelChangeRequestSelection\(\)/);
  assert.match(app, /if \(overviewSelectionScopeChanged\(eventFilter, filter\)\) cancelEventSelection\(\)/);
  assert.match(app, /const selectedItems = props.selectedChangeRequests/);
  assert.match(app, /const selectedEventItems = props.selectedEvents/);
  assert.match(app, /setSelectedChangeRequests\(\(current\) => refreshOverviewSelection\(current, changeRequests/);
  assert.match(app, /setSelectedEvents\(\(current\) => refreshOverviewSelection\(current, events/);
  const latest = app.slice(app.indexOf("function requestTriggerLatestEvents("), app.indexOf("function requestReplayEvent("));
  assert.match(latest, /const targets = items;/);
  assert.doesNotMatch(latest, /items.filter/);
});

test("部分失败从确认目标保留记录，不能从当前页重建失败选择", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const confirmation = app.slice(app.indexOf("async function confirmOverviewAction("), app.indexOf("const enabledRepositories = useMemo("));
  assert.match(confirmation, /setSelectedChangeRequests\(\s*changeRequestItems\s*\.filter\(\(target\) => failedKeys.has/);
  assert.match(confirmation, /setSelectedEvents\(eventItems.filter\(\(event\) => failedIds.has/);
  assert.doesNotMatch(confirmation, /setSelectedChangeRequests\(changeRequests|setSelectedEvents\(events/);
  assert.match(confirmation, /targets: changeRequestItems.map/);
  assert.match(confirmation, /event_ids: eventItems.map/);
});
