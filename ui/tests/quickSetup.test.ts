import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { initialSetupRules, setupAgents, setupRuleDescription, setupRules } from "../src/quickSetup.ts";
import type { ConfigDocument } from "../src/types.ts";

// 数据刻意使用非内置名称和跨类别同名规则，避免向导隐式绑定模板。
const document = {
  rules: [
    { name: "future-rule", events: ["change_request.opened"], agents: ["root"] },
    { name: "limited", events: [], agents: ["child"], repositories: ["old"] },
    { name: "off", events: [], agents: [], enabled: false },
  ],
  scheduled_rules: [{ name: "future-rule", agents: ["root"], schedule: { kind: "interval", interval_value: 2, interval_unit: "days" } }],
  agents: { root: { allowed_sub_agents: ["child"] }, child: { allowed_sub_agents: ["root"] } },
} as unknown as ConfigDocument;

test("动态展示全部规则，不混淆两类同名规则", () => {
  const rules = setupRules(document);
  assert.equal(rules.length, 4);
  assert.equal(new Set(rules.map((item) => item.key)).size, 4);
  assert.equal(setupRuleDescription(rules[3]), "每 2 天");
  assert.deepEqual(initialSetupRules(rules), ["event:future-rule", "scheduled:future-rule"]);
  assert.deepEqual(initialSetupRules(rules, "old"), ["event:future-rule", "event:limited", "scheduled:future-rule"]);
});

test("Skill 分配包含 sub-agent，循环和重复引用去重", () => {
  assert.deepEqual(setupAgents(document, ["event:future-rule", "scheduled:future-rule"]), ["root", "child"]);
  assert.deepEqual(setupAgents(document, []), []);
});

test("向导草稿不写本地存储，不调用即时执行 API，取消与返回不保存", () => {
  const source = readFileSync(new URL("../src/QuickSetupWizard.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(source, /localStorage|sessionStorage|\/api\/control\/scan|\/api\/runs/);
  assert.match(source, /\/api\/setup\/preview/);
  assert.match(source, /\/api\/setup\/complete/);
  assert.match(source, /aria-label=\{showToken/);
  assert.match(source, /showModal\(\)/);
  assert.match(source, /const steps = \["选择平台", "仓库与凭证", "选择规则", "配置 Skill", "检查与完成"\]/);
});
