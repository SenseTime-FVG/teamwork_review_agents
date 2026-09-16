// 校验签名语言控件与配置的接线，不在语言选项后增加辅助说明。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const types = readFileSync(new URL("../src/types.ts", import.meta.url), "utf8");
const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const control = app.match(/<SelectControl\s+ariaLabel="模型签名语言"[\s\S]*?\/>/)?.[0] ?? "";

test("签名语言只有中文、English、双语，不显示额外标题或提示", () => {
  assert.ok(control);
  const options = Array.from(control.matchAll(/value:\s*"([^"]+)",\s*label:\s*"([^"]+)"/g), (match) => [match[1], match[2]]);
  assert.deepEqual(options, [["zh", "中文"], ["en", "English"], ["bilingual", "双语"]]);
  assert.doesNotMatch(control, /\b(?:label|help|title|description)=/);
  assert.match(control, /ariaLabel="模型签名语言"/);
});

test("新建与旧 Agent 默认中文，关闭开关时禁用选择但保留语言值", () => {
  assert.match(app, /managed_comment_model_signature_language: "zh"/);
  assert.match(control, /value=\{agent\.managed_comment_model_signature_language \?\? "zh"\}/);
  assert.match(control, /disabled=\{!agent\.managed_comment \|\| !agent\.managed_comment_model_signature\}/);
  assert.match(control, /onChange=\{\(language\) => update\(name,\s*\{\s*managed_comment_model_signature_language: language/);
  assert.match(types, /managed_comment_model_signature_language\?: "zh" \| "en" \| "bilingual"/);
});

test("签名开关与语言框相邻，允许窄屏换行且使用紧凑字体", () => {
  assert.match(app, /className="managed-comment-signature-controls"[\s\S]*?label="附加模型签名"[\s\S]*?ariaLabel="模型签名语言"/);
  assert.match(styles, /\.managed-comment-signature-controls\s*\{[^}]*flex-wrap:\s*wrap/);
  assert.match(styles, /\.managed-comment-language\s*\{[^}]*width:\s*108px/);
  assert.match(styles, /\.managed-comment-language \.select-combobox-trigger\s*\{[^}]*font-size:\s*11px/);
});
