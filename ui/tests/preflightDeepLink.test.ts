import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { preflightRunIdFromHash, withoutPreflightHash } from "../src/preflightDeepLink.ts";

test("CI 深链接读取具体运行 ID，不受分页、筛选或其他片段参数影响", () => {
  const id = "1637c7da-5940-481b-b487-8a57e2909e37";
  for (const hash of [`#preflight=${id}`, `#other=value&preflight=${id}`, `preflight=${id}`]) {
    assert.equal(preflightRunIdFromHash(hash), id);
  }
});

test("拒绝缺失、重复、超长及可注入路径的运行 ID，不把凭据当作运行目标", () => {
  for (const hash of [
    "", "#token=secret", "#preflight=", "#preflight=a&preflight=b",
    "#preflight=../config", "#preflight=%2Fapi%2Fconfig", "#preflight=https://example.com",
    "#preflight=has%20space", "#preflight=%00", `#preflight=${"a".repeat(129)}`,
  ]) assert.equal(preflightRunIdFromHash(hash), null);
});

test("关闭 CI 详情只移除定位参数，不丢失路径、查询或其他片段参数", () => {
  assert.equal(withoutPreflightHash("https://review.example.com/teamwork/?page=2#preflight=run-123"), "https://review.example.com/teamwork/?page=2");
  assert.equal(withoutPreflightHash("https://review.example.com/#other=value&preflight=run-123"), "https://review.example.com/#other=value");
  assert.equal(withoutPreflightHash("https://review.example.com/"), "https://review.example.com/");
});

test("深链接使用现有认证后的 CI 详情与实时日志，不依赖执行列表，不覆盖编辑草稿", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(app, /useState<Tab>\(\(\) => preflightRunIdFromHash\(window.location.hash\) \? "runs" : "overview"\)/);
  assert.match(app, /!loading && document && linkedPreflightId &&/);
  assert.match(app, /key=\{linkedPreflightId\}\s+runId=\{linkedPreflightId\}/);
  assert.match(app, /window.history.replaceState\(window.history.state, "", withoutPreflightHash\(window.location.href\)\)/);
  const listener = app.slice(app.indexOf("const openLinkedPreflight"), app.indexOf("const [document, setDocument]"));
  assert.match(listener, /addEventListener\("hashchange", openLinkedPreflight\)/);
  assert.match(listener, /removeEventListener\("hashchange", openLinkedPreflight\)/);
  assert.doesNotMatch(listener, /setTab|setDocument|setEditing|setDirty|setToken/);
  assert.match(app, /label="对外访问地址（可选）"/);
  const drawer = app.slice(app.indexOf("function PreflightRunDetailDrawer("), app.indexOf("function AgentRunDetailDrawer("));
  assert.match(drawer, /api<PreflightRunDetail>\(`\/api\/preflight-runs\/\$\{encodeURIComponent\(runId\)\}/);
  assert.match(drawer, /streamPreflightLogs\(/);
  const api = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
  assert.match(api, /result.set\("Authorization", `Bearer \$\{token\}`\)/);
});
