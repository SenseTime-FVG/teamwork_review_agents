import type { CodexRuntimeOptions, ConfigDocument, ModelProviderConfig } from "./types.ts";

// 外部服务的候选值不等于能力声明，是否支持由上游请求结果确认。
export const EXTERNAL_REASONING_LEVELS = ["low", "medium", "high", "xhigh", "max"];

export function reasoningEffortOptions(
  levels: readonly string[],
  current?: string | null,
): string[] {
  // 不再提供 minimal，但保留其他历史自定义值，避免编辑时丢失配置。
  return Array.from(new Set([
    ...levels,
    ...(current ? [current] : []),
  ])).filter((value) => value !== "minimal");
}

export function modelSupportsReasoningEffort(
  provider?: ModelProviderConfig,
  model?: string | null,
): boolean {
  if (!provider || !model?.trim()) return false;
  if (provider.driver === "codex_cli") return true;
  return (
    (provider.driver === "openai_responses" || provider.driver === "openai_chat_completions")
    && model.trim().toLowerCase().startsWith("gpt-")
  );
}

type InheritedEffort = { value: string | null; source: string; fromAgent?: boolean };

function inheritedEffort(
  document: ConfigDocument,
  options: CodexRuntimeOptions,
  provider: ModelProviderConfig,
  model: string,
  agentEffort?: string | null,
): InheritedEffort {
  // 每个回退候选独立解析，不能套用全局主模型或另一个 Provider 的默认。
  if (agentEffort) return { value: agentEffort, source: "Agent 显式配置", fromAgent: true };
  if (provider.model_reasoning_effort) {
    return { value: provider.model_reasoning_effort, source: `Provider ${provider.display_name}` };
  }
  if (provider.driver !== "codex_cli") return { value: null, source: "上游未声明默认 effort" };
  const runtime = document.runtime.codex;
  if (runtime?.model_reasoning_effort) {
    return { value: runtime.model_reasoning_effort, source: "Codex 运行时配置" };
  }
  // 基座模式不采用完整 CLI 的分层配置或模型目录默认，保持与运行器一致。
  if (runtime?.execution_mode !== "cli") {
    const base = options.model_base_reasoning_effort;
    return base?.known && base.value
      ? { value: base.value, source: base.source === "builtin" ? "Teamwork 基座默认" : "Codex 用户配置" }
      : { value: null, source: "基座默认值尚未解析" };
  }
  const configured = options.inherited_settings?.model_reasoning_effort;
  if (configured?.known && configured.value) {
    return { value: configured.value, source: configured.source === "user" ? "Codex 用户配置" : "Codex 有效配置" };
  }
  const modelDefault = options.models.find((item) => item.slug === model)?.default_reasoning_level;
  return modelDefault
    ? { value: modelDefault, source: `模型 ${model} 默认` }
    : { value: null, source: "Codex / 模型默认尚未解析" };
}

export function reasoningEffortPresentation(
  document: ConfigDocument,
  options: CodexRuntimeOptions,
  provider: ModelProviderConfig | undefined,
  model: string | null | undefined,
  explicit?: string | null,
  agentEffort?: string | null,
): { inheritLabel: string; help: string } {
  if (!modelSupportsReasoningEffort(provider, model)) {
    return {
      inheritLabel: model ? "不适用（当前模型不支持）" : "默认值未知（模型尚未解析）",
      help: model ? "当前模型不支持推理 effort，不发送该参数" : "先选择或解析模型后才能确定 effort",
    };
  }
  const inherited = inheritedEffort(document, options, provider!, model!, agentEffort);
  const value = explicit || inherited.value;
  const source = explicit ? "当前项显式配置" : inherited.source;
  return {
    inheritLabel: inherited.value
      ? `跟随 ${inherited.fromAgent ? "Agent 配置" : "Provider 默认"}（${inherited.value}）`
      : "上游默认（未知）",
    help: `当前有效：${value ?? "未知"} · 来源：${source}`,
  };
}
