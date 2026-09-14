import type { ConfigDocument, Rule, ScheduledRule } from "./types";

export type SetupRule = { key: string; kind: "event" | "scheduled"; rule: Rule | ScheduledRule };

// 两类规则使用各自稳定名称，不依赖内置规则数量或数组下标。
export function setupRules(document: ConfigDocument): SetupRule[] {
  return [
    ...document.rules.map((rule) => ({ key: `event:${rule.name}`, kind: "event" as const, rule })),
    ...document.scheduled_rules.map((rule) => ({ key: `scheduled:${rule.name}`, kind: "scheduled" as const, rule })),
  ];
}

// 新仓库默认只继承已启用的“全部”；已有仓库按实际范围回填。
export function initialSetupRules(rules: SetupRule[], repositoryId?: string): string[] {
  return rules.filter(({ rule }) => rule.enabled !== false && (
    !rule.repositories?.length || Boolean(repositoryId && rule.repositories.includes(repositoryId))
  )).map(({ key }) => key);
}

// 展开实际允许委托的 Agent，循环和多个根规则引用同一 Agent 时均去重。
export function setupAgents(document: ConfigDocument, selected: string[]): string[] {
  const pending = setupRules(document).filter(({ key }) => selected.includes(key)).flatMap(({ rule }) => rule.agents);
  const seen = new Set<string>();
  while (pending.length) {
    const name = pending.shift()!;
    if (seen.has(name)) continue;
    seen.add(name);
    pending.push(...(document.agents[name]?.allowed_sub_agents ?? []));
  }
  return [...seen];
}

// 保留完整触发信息，定时规则无需用户重新输入周期。
export function setupRuleDescription(item: SetupRule): string {
  if (item.kind === "event") return (item.rule as Rule).events.join("、");
  const schedule = (item.rule as ScheduledRule).schedule;
  if (schedule.kind === "cron") return `${schedule.cron} · ${schedule.timezone ?? "Asia/Shanghai"}`;
  return `每 ${schedule.interval_value ?? 1} ${{ minutes: "分钟", hours: "小时", days: "天" }[schedule.interval_unit ?? "hours"]}`;
}
