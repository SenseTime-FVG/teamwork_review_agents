import assert from "node:assert/strict";
import test from "node:test";
import { formatContextWindow, globalContextWindow, inheritedAgentWindow, inheritedProviderWindow, providerContextWindow, contextWindowSourceLabel } from "../src/contextWindow.ts";
import type { ConfigDocument } from "../src/types.ts";

// 只填入窗口解析依赖的字段，不依赖真实配置或账号。
const document = {
  model_providers: {
    a: { display_name: "A", driver: "codex_cli", context_window_tokens: 100000 },
    b: { display_name: "B", driver: "openai_responses", context_window_tokens: 200000 },
    empty: { display_name: "C", driver: "openai_responses" },
  },
  runtime: { default_model: { provider: "a", context_window_tokens: 150000 } },
} as ConfigDocument;

test("Provider 留空显示具体系统默认，数字格式明确", () => {
  assert.deepEqual(providerContextWindow(document.model_providers.empty), { tokens: 272000, source: "系统默认" });
  assert.equal(formatContextWindow(272000), "272,000 tokens");
  assert.deepEqual(inheritedProviderWindow(document, "empty"), { tokens: 272000, source: "Provider C / 系统默认" });
});

test("Agent 继承全局或显式选择 Provider 使用不同窗口", () => {
  assert.deepEqual(globalContextWindow(document), { tokens: 150000, source: "全局默认模型" });
  assert.equal(inheritedAgentWindow(document, {}).tokens, 150000);
  assert.equal(inheritedAgentWindow(document, { model_provider: "b" }).tokens, 200000);
  assert.equal(inheritedAgentWindow(document, { model_provider: "a" }).tokens, 100000);
});

test("主模型和回退的继承显示随上游变化，不修改草稿", () => {
  const draft = structuredClone(document);
  draft.runtime.default_model!.context_window_tokens = null;
  const before = JSON.stringify(draft);
  assert.equal(globalContextWindow(draft).tokens, 100000);
  assert.equal(inheritedProviderWindow(draft, "b").tokens, 200000);
  assert.equal(JSON.stringify(draft), before);
  draft.model_providers.b.context_window_tokens = 210000;
  assert.equal(inheritedAgentWindow(draft, { model_provider: "b" }).tokens, 210000);
  draft.model_providers.b.context_window_tokens = null;
  assert.equal(inheritedProviderWindow(draft, "b").tokens, 272000);
  assert.equal(draft.runtime.default_model!.context_window_tokens, null);
});

test("运行快照来源能显示可读来源并兼容历史记录", () => {
  assert.equal(contextWindowSourceLabel("provider:b"), "Provider b");
  assert.equal(contextWindowSourceLabel("agent_fallback"), "Agent 回退项");
  assert.equal(contextWindowSourceLabel(undefined), "未记录");
});
