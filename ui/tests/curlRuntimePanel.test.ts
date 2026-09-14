import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// 校验组件与共享样式的接线，避免漏掉基础类后退回浏览器原生白色按钮。
const component = readFileSync(new URL("../src/CurlRuntimePanel.tsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("curl 准备按钮复用暗色按钮及悬停、禁用样式，准备中保持禁用", () => {
  const button = component.match(/<button\b[\s\S]*?<\/button>/)?.[0];
  assert.ok(button, "运行环境面板必须保留准备按钮");
  const classes = button.match(/className="([^"]+)"/)?.[1].split(/\s+/) ?? [];
  assert.ok(classes.includes("button"), "必须包含共享按钮基础类");
  assert.ok(classes.includes("secondary"), "必须包含暗色次级按钮变体");
  assert.match(button, /disabled=\{preparing\}/);
  assert.match(button, /aria-busy=\{preparing\}/);
  assert.match(button, /preparing\s*\?\s*"正在准备…"\s*:\s*"重新检查并准备"/);
  assert.match(styles, /\.button\.secondary\s*\{[^}]*background:/);
  assert.match(styles, /\.button\.secondary:hover:not\(:disabled\)\s*\{/);
  assert.match(styles, /\.button:disabled\s*\{/);
});

test("离线包链接在暗色背景保留正常、已访问、悬停和键盘焦点样式", () => {
  assert.match(styles, /\.curl-runtime-panel a,\s*\.curl-runtime-panel a:visited\s*\{[^}]*color:\s*var\(--blue\)/);
  assert.match(styles, /\.curl-runtime-panel a:hover\s*\{[^}]*color:\s*var\(--bright\)/);
  assert.match(styles, /\.curl-runtime-panel \.button:focus-visible,[^}]*\.curl-runtime-panel a:focus-visible,[^}]*outline:\s*2px solid var\(--blue\)/);
});
