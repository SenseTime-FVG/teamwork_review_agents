# 仓库级 Skill 白名单设计

## 背景

Teamwork 当前只在 Agent 上配置 Skill 列表。同一个 Agent 被多个仓库触发时会装载相同 Skill，仓库管理员无法进一步约束当前项目允许使用的能力。

## 目标

- 为每个仓库增加独立的 Skill 策略。
- 运行时只装载 Agent Skill 白名单与仓库白名单的交集。
- 根 Agent 与 sub-agent 都使用各自 Agent 配置和当前仓库策略重新计算交集。
- 兼容没有仓库 Skill 配置的现有 YAML。

## 配置语义

仓库字段使用 `allowed_skills`，并保留三种明确状态：

- 字段缺失或 `allowed_skills: []`：不额外限制，继续使用 Agent 自身的 Skill 列表。
- 非空列表：仅允许列表中的 Skill，最终按 Agent 列表顺序取得交集。
- `allowed_skills: null`：禁止当前仓库的所有 Skill。

例如 Agent 配置 `skills: [dependency-review, incremental-doc-update]`，仓库配置 `allowed_skills: [incremental-doc-update, security-review]`，最终只装载 `incremental-doc-update`。

仓库非空白名单中的 ID 必须存在于全局 `skills` 配置。空列表和 `null` 都不产生引用校验错误。

## 管理界面

仓库详情使用独立的单选策略卡片展示三种状态：

1. “不额外限制”：默认状态，使用 Agent 白名单。
2. “仅允许所选”：展开全局 Skill 多选列表，至少选择一项。
3. “禁止所有 Skill”：当前仓库的所有 Agent 都不装载 Skill。

进入“仅允许所选”时默认选中当前配置中的全部 Skill，用户可以继续收窄；不允许取消最后一项，应改选“禁止所有 Skill”。Skill 重命名时同步更新仓库非空白名单；删除最后一个被允许的 Skill 时把对应仓库策略收敛为 `null`，避免意外从限制模式变成不额外限制。

## 运行时边界

Codex CLI 模式只把交集内 Skill 标记为启用。模型基座模式只把交集内 `SKILL.md` 注入模型指令，并以交集数量记录运行日志。

Skill 投影继续覆盖应用配置中的 Skill，以兼容 sub-agent 继承父工作区但使用不同 Agent 白名单的现有机制。仓库策略与 Agent 策略控制运行时装载，不把 Skill 文件本身作为机密隔离边界。

## 非目标

- 不改变全局 Skill 的创建、导入和存储方式。
- 不让触发规则覆盖仓库 Skill 策略。
- 不让 sub-agent 继承父 Agent 的 Skill 列表。
