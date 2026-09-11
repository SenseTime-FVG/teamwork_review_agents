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
