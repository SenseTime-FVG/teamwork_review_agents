import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { createCodexCatalogRefresher, codexCatalogSourceLabel } from "../src/codexCatalogRefresh.ts";
import type { CodexCatalogState } from "../src/codexCatalogRefresh.ts";
import type { CodexRuntimeOptions } from "../src/types.ts";

// 只构造目录协调器依赖字段，不接触账号、网络或配置文件。
const empty = { models: [], catalog_source: "unavailable" } as unknown as CodexRuntimeOptions;
const fresh = { models: [{ slug: "new-model" }], catalog_source: "app_server" } as unknown as CodexRuntimeOptions;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test("同一配置自动/手动刷新合并，完成后可再次刷新", async () => {
  const request = deferred<CodexRuntimeOptions>();
  const states: CodexCatalogState[] = [];
  let calls = 0;
  const controller = createCodexCatalogRefresher(empty, () => { calls++; return request.promise; }, (state) => states.push(state));
  const first = controller.refresh("revision-a");
  assert.equal(first, controller.refresh("revision-a"));
  assert.equal(states.at(-1)?.busy, true);
  request.resolve(fresh);
  await first;
  assert.equal(calls, 1);
  assert.equal(states.at(-1)?.options, fresh);
  assert.equal(states.at(-1)?.busy, false);
  await controller.refresh("revision-a");
  assert.equal(calls, 2);
});

test("旧配置的迟到响应不能覆盖新配置目录", async () => {
  const oldRequest = deferred<CodexRuntimeOptions>();
  const newRequest = deferred<CodexRuntimeOptions>();
  const requests = [oldRequest, newRequest];
  const states: CodexCatalogState[] = [];
  const controller = createCodexCatalogRefresher(empty, () => requests.shift()!.promise, (state) => states.push(state));
  const old = controller.refresh("old-home");
  await Promise.resolve();
  const current = controller.refresh("new-home");
  newRequest.resolve(fresh);
  await current;
  oldRequest.resolve({ ...fresh, models: [] });
  await old;
  assert.equal(states.at(-1)?.options, fresh);
  assert.equal(states.at(-1)?.busy, false);
});

test("同一配置网络失败保留原目录，切换配置后不能借用旧账号目录", async () => {
  let fail = false;
  const states: CodexCatalogState[] = [];
  const controller = createCodexCatalogRefresher(empty, async () => {
    if (fail) throw new Error("敏感响应内容");
    return fresh;
  }, (state) => states.push(state));
  await controller.refresh("first");
  fail = true;
  await controller.refresh("first");
  assert.equal(states.at(-1)?.options, fresh);
  assert.match(states.at(-1)!.error, /保留上次结果/);
  assert.doesNotMatch(states.at(-1)!.error, /敏感/);
  await controller.refresh("second");
  assert.equal(states.at(-1)?.options, empty);
  assert.ok(states.at(-1)?.error);
});

test("成功空目录覆盖旧模型，失败回退来源明确", async () => {
  const states: CodexCatalogState[] = [];
  const controller = createCodexCatalogRefresher(fresh, async () => ({ ...fresh, models: [] }), (state) => states.push(state));
  await controller.refresh("same");
  assert.deepEqual(states.at(-1)?.options.models, []);
  assert.equal(codexCatalogSourceLabel(fresh), "当前 CLI · model/list");
  assert.match(codexCatalogSourceLabel({ ...fresh, catalog_source: "account_cache" }), /缓存.*回退/);
  assert.match(codexCatalogSourceLabel({ ...fresh, catalog_source: "bundled" }), /参考目录.*回退/);
});

test("自动刷新接线与只读按钮不修改表单或已选模型", () => {
  const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const editor = source.slice(source.indexOf("function ModelProvidersEditor"), source.indexOf("function CodexRuntimeEditor"));
  assert.match(editor, /selectedId === null \|\| selectedId === "codex-cli"/);
  assert.match(editor, /void props\.onRefreshCodex\(\)/);
  const runtimeEditor = source.slice(source.indexOf("function CodexRuntimeEditor"));
  assert.ok(runtimeEditor.indexOf("刷新模型") < runtimeEditor.indexOf('<fieldset className="config-editor-surface"'));
  assert.match(runtimeEditor, /disabled=\{props\.refreshing\}/);
  const coordinator = source.slice(source.indexOf("const codexCatalogRefresher ="), source.indexOf("const [editing, setEditing]", source.indexOf("const codexCatalogRefresher =")));
  assert.match(coordinator, /cache: "no-store"/);
  assert.match(coordinator, /if \(revision\) void refreshCodexCatalog\(\)/);
  assert.doesNotMatch(coordinator, /setDocument|setSavedDocument|setDraftDocument|setInterval/);
  assert.match(source, /onRefreshCodex=\{refreshCodexCatalog\}/);
});
