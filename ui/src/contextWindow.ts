import type { Agent, ConfigDocument, ModelProviderConfig } from "./types";

// 与后端保持一致；继承值只参与显示，不能写回可空的配置字段。
export const DEFAULT_CONTEXT_WINDOW_TOKENS = 272000;
export type ContextWindow = { tokens: number; source: string };

export function formatContextWindow(tokens: number): string {
  return `${tokens.toLocaleString("en-US")} tokens`;
}

export function providerContextWindow(provider?: ModelProviderConfig, providerId = "Provider"): ContextWindow {
  return {
    tokens: provider?.context_window_tokens ?? DEFAULT_CONTEXT_WINDOW_TOKENS,
    source: provider?.context_window_tokens == null ? "系统默认" : `Provider ${provider.display_name || providerId}`,
  };
}

export function inheritedProviderWindow(document: ConfigDocument, providerId: string): ContextWindow {
  const provider = document.model_providers[providerId];
  const result = providerContextWindow(provider, providerId);
  return { tokens: result.tokens, source: `Provider ${provider?.display_name || providerId}${provider?.context_window_tokens == null ? " / 系统默认" : ""}` };
}

export function globalContextWindow(document: ConfigDocument): ContextWindow {
  const selection = document.runtime.default_model ?? { provider: "codex-cli" };
  const inherited = inheritedProviderWindow(document, selection.provider);
  return {
    tokens: selection.context_window_tokens ?? inherited.tokens,
    source: selection.context_window_tokens == null ? `全局默认 / ${inherited.source}` : "全局默认模型",
  };
}

export function inheritedAgentWindow(document: ConfigDocument, agent: Agent): ContextWindow {
  // 即使显式选中的 Provider 与全局相同，也不继承全局模型的窗口覆盖。
  return agent.model_provider
    ? inheritedProviderWindow(document, agent.model_provider)
    : globalContextWindow(document);
}

export function contextWindowSourceLabel(source?: string | null): string {
  if (source?.startsWith("provider:")) return `Provider ${source.slice(9)}`;
  return ({ agent: "Agent 配置", global: "全局默认模型", agent_fallback: "Agent 回退项", global_fallback: "全局回退项", system_default: "系统默认" } as Record<string, string>)[source ?? ""] ?? source ?? "未记录";
}
