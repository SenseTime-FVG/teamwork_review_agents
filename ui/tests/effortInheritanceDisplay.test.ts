import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { reasoningEffortPresentation } from "../src/reasoningEffort.ts";
import type { CodexRuntimeOptions, ConfigDocument, ModelProviderConfig } from "../src/types.ts";

// 只提供展示所需字段，避免测试依赖任何真实账户配置。
function fixture() {
  const codex = { driver: "codex_cli", display_name: "Codex CLI" } as ModelProviderConfig;
  const api = { driver: "openai_responses", display_name: "API", model_reasoning_effort: "high" } as ModelProviderConfig;
  const document = {
    runtime: { codex: { execution_mode: "model" }, default_model: { provider: "codex-cli", reasoning_effort: "max" } },
    model_providers: { "codex-cli": codex, api },
  } as ConfigDocument;
  const options = {
    inherited_settings: { model_reasoning_effort: { value: "xhigh", known: true, source: "codex" } },
    model_base_reasoning_effort: { value: "medium", known: true, source: "builtin" },
    models: [
      { slug: "gpt-one", default_reasoning_level: "high" },
      { slug: "gpt-two", default_reasoning_level: "low" },
    ],
  } as CodexRuntimeOptions;
  return { codex, api, document, options };
}

test("Provider 继承项展示具体 effort 与来源，显式项不改写继承提示", () => {
  const { document, options, api } = fixture();
  assert.deepEqual(reasoningEffortPresentation(document, options, api, "gpt-api"), {
    inheritLabel: "跟随 Provider 默认（high）", help: "当前有效：high · 来源：Provider API",
  });
  assert.deepEqual(reasoningEffortPresentation(document, options, api, "gpt-api", "ultra"), {
    inheritLabel: "跟随 Provider 默认（high）", help: "当前有效：ultra · 来源：当前项显式配置",
  });
});

test("Codex Provider 和运行时草稿的显式配置依次优先", () => {
  const { document, options, codex } = fixture();
  document.runtime.codex!.model_reasoning_effort = "max";
  codex.model_reasoning_effort = "high";
  assert.match(reasoningEffortPresentation(document, options, codex, "gpt-one").help, /high.*Provider Codex CLI/);
  delete codex.model_reasoning_effort;
  assert.match(reasoningEffortPresentation(document, options, codex, "gpt-one").help, /max.*运行时配置/);
  delete document.runtime.codex!.model_reasoning_effort;
  assert.match(reasoningEffortPresentation(document, options, codex, "gpt-one").help, /medium.*基座默认/);
});

test("基座模式不会误用 CLI 有效配置或模型目录默认", () => {
  const { document, options, codex } = fixture();
  assert.equal(reasoningEffortPresentation(document, options, codex, "gpt-one").inheritLabel, "跟随 Provider 默认（medium）");
  options.model_base_reasoning_effort = { value: "low", source: "user", known: true };
  assert.match(reasoningEffortPresentation(document, options, codex, "gpt-one").help, /low.*用户配置/);
  delete options.model_base_reasoning_effort;
  assert.equal(reasoningEffortPresentation(document, options, codex, "gpt-one").inheritLabel, "上游默认（未知）");
});

test("完整 CLI 依次使用有效配置和当前模型目录，切换模型即时更新", () => {
  const { document, options, codex } = fixture();
  document.runtime.codex!.execution_mode = "cli";
  assert.match(reasoningEffortPresentation(document, options, codex, "gpt-one").help, /xhigh.*有效配置/);
  options.inherited_settings.model_reasoning_effort = { value: null, source: "unknown", known: false };
  assert.match(reasoningEffortPresentation(document, options, codex, "gpt-one").help, /high.*gpt-one 默认/);
  assert.match(reasoningEffortPresentation(document, options, codex, "gpt-two").help, /low.*gpt-two 默认/);
  assert.equal(reasoningEffortPresentation(document, options, codex, "custom-model").inheritLabel, "上游默认（未知）");
});

test("外部 Provider 不继承 Codex 或全局主模型 effort，无声明保持未知", () => {
  const { document, options, api } = fixture();
  delete api.model_reasoning_effort;
  document.runtime.codex!.model_reasoning_effort = "ultra";
  assert.deepEqual(reasoningEffortPresentation(document, options, api, "gpt-one"), {
    inheritLabel: "上游默认（未知）", help: "当前有效：未知 · 来源：上游未声明默认 effort",
  });
});

test("Agent 回退先继承 Agent 显式 effort，回退项可再次覆盖", () => {
  const { document, options, api } = fixture();
  assert.deepEqual(reasoningEffortPresentation(document, options, api, "gpt-one", undefined, "max"), {
    inheritLabel: "跟随 Agent 配置（max）", help: "当前有效：max · 来源：Agent 显式配置",
  });
  assert.deepEqual(reasoningEffortPresentation(document, options, api, "gpt-one", "low", "max"), {
    inheritLabel: "跟随 Agent 配置（max）", help: "当前有效：low · 来源：当前项显式配置",
  });
});

test("每个回退候选独立解析，草稿与继承空值保持原样", () => {
  const { document, options, api, codex } = fixture();
  const selections = [
    { provider: "api", model: "gpt-one", reasoning_effort: null },
    { provider: "codex-cli", model: "gpt-two" },
  ];
  const before = JSON.stringify({ document, options, selections });
  assert.match(reasoningEffortPresentation(document, options, api, selections[0].model, selections[0].reasoning_effort).inheritLabel, /high/);
  assert.match(reasoningEffortPresentation(document, options, codex, selections[1].model, selections[1].reasoning_effort).inheritLabel, /medium/);
  assert.equal(JSON.stringify({ document, options, selections }), before);
});

test("不支持或尚未解析的模型不显示伪造的 effort", () => {
  const { document, options, api } = fixture();
  assert.match(reasoningEffortPresentation(document, options, api, "deepseek-test", "high").inheritLabel, /不适用/);
  assert.match(reasoningEffortPresentation(document, options, api, undefined).inheritLabel, /模型尚未解析/);
  assert.match(reasoningEffortPresentation(document, options, { ...api, driver: "gemini_generate_content" }, "gpt-test", "high").help, /不发送/);
});

test("全局默认和两级回退共用动态展示，不把解析值写入配置", () => {
  const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /label: defaultEffortDisplay\.inheritLabel/);
  assert.match(source, /label: effortDisplay\.inheritLabel/);
  assert.match(source, /help=\{effortDisplay\.help\}/);
  assert.match(source, /agentReasoningEffort=\{agent\.model_reasoning_effort\}/);
  assert.match(source, /reasoning_effort: reasoning_effort \|\| undefined/);
  assert.doesNotMatch(source, /label: "跟随 Provider 默认"/);
});
