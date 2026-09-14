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

test("凭据候选仍可主动选用，仓库信息与密码管理器账号分属独立表单", () => {
  const source = readFileSync(new URL("../src/QuickSetupWizard.tsx", import.meta.url), "utf8");
  assert.match(source, /<form[^\n]*aria-label="仓库信息"/);
  assert.match(source, /<form[^\n]*aria-label="平台凭据"/);
  assert.match(source, /name="platform-token-account" autoComplete="username"/);
  assert.match(source, /name="platform-access-token" autoComplete="current-password"/);
  assert.doesNotMatch(source, /autoComplete="new-password"/);
  assert.match(source, /key=\{`name-\$\{connectionVersion\}`\}/);
  assert.match(source, /key=\{`token-\$\{connectionVersion\}`\}/);
});

test("初始预填不进入草稿，真实点击或按键才解锁，未使用定时清空", () => {
  const source = readFileSync(new URL("../src/SetupIntentInput.tsx", import.meta.url), "utf8");
  assert.match(source, /useRef\(false\)/);
  assert.match(source, /readOnly=\{!editable\}/);
  assert.match(source, /onPointerDown=/);
  assert.match(source, /onKeyDown=/);
  assert.match(source, /nativeEvent\.isTrusted/);
  assert.match(source, /if \(interacted.current\) onValueChange/);
  assert.match(source, /onAnimationStart=/);
  assert.doesNotMatch(source, /onFocus=|setInterval|setTimeout|localStorage|sessionStorage/);
});

test("退出使用独立深色确认框，安全焦点和 Esc 均返回配置", () => {
  const wizard = readFileSync(new URL("../src/QuickSetupWizard.tsx", import.meta.url), "utf8");
  const dialog = readFileSync(new URL("../src/SetupExitConfirmation.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(wizard, /window\.confirm/);
  assert.match(wizard, /if \(!busy\) setConfirmExit\(true\)/);
  assert.match(dialog, /role="alertdialog"/);
  assert.match(dialog, /showModal\(\)/);
  assert.match(dialog, /continueButton.current\?\.focus\(\)/);
  assert.match(dialog, /onCancel=[\s\S]*?event\.preventDefault\(\);[\s\S]*?onContinue\(\);/);
  assert.match(dialog, /previousFocus\.focus\(/);
  assert.match(dialog, /event.key !== "Tab"/);
  assert.match(dialog, /event.shiftKey/);
  assert.match(dialog, /继续配置/);
  assert.match(dialog, /放弃并退出/);
});
