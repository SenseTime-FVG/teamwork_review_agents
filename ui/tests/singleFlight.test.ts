import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createSingleFlight } from "../src/singleFlight.ts";

function deferred<T>() {
  // 屏障模拟慢请求，验证轮询重叠和失败恢复，无需真实网络。
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test("慢请求期间多次轮询和主动刷新共用请求，结束后重新读取", async () => {
  const response = deferred<number>();
  let calls = 0;
  const refresh = createSingleFlight(() => { calls++; return response.promise; });
  const first = refresh();
  for (let index = 0; index < 10; index++) assert.equal(refresh(), first);
  await Promise.resolve();
  assert.equal(calls, 1);
  response.resolve(123);
  assert.equal(await first, 123);
  assert.equal(await refresh(), 123);
  assert.equal(calls, 2);
});

test("异步失败与同步抛错均释放请求，下一轮可以恢复", async () => {
  const response = deferred<number>();
  let calls = 0;
  const refresh = createSingleFlight(() => {
    calls++;
    if (calls === 1) return response.promise;
    if (calls === 2) throw new Error("同步错误");
    return Promise.resolve(456);
  });
  const pending = refresh();
  assert.equal(refresh(), pending);
  response.reject(new Error("临时网络错误"));
  await assert.rejects(pending, /临时网络错误/);
  await assert.rejects(refresh(), /同步错误/);
  assert.equal(await refresh(), 456);
  assert.equal(calls, 3);
});

test("仓库保存反馈不等待列表，三个定时刷新入口在保存期间暂停", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const save = app.slice(app.indexOf("async function saveRepository()"), app.indexOf("async function deleteRepository()"));
  assert.match(save, /void refreshWorkspaceItems\(\)/);
  assert.doesNotMatch(save, /await refreshWorkspaceItems\(\)/);
  const polling = app.slice(app.indexOf("const refreshWorkspaceItems ="), app.indexOf("const repositories =", app.indexOf("const refreshWorkspaceItems =")));
  assert.equal((polling.match(/createSingleFlight\(/g) ?? []).length, 3);
  assert.match(polling, /if \(saving\) return/);
  assert.match(polling, /!detailId \|\| creating \|\| saving/);
  assert.match(polling, /!detailId \|\| editing \|\| creating \|\| saving/);
});
