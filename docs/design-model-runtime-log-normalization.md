# 模型基座运行日志语义统一设计

## 背景

Codex CLI 会直接输出 `thread.started`、`item.started`、`item.completed` 和 `turn.completed` 等 Agent 语义事件。模型基座模式直接消费 Codex Responses SSE，上游事件名称是 `response.created`、`response.output_item.done` 和 `response.completed`。这些名称属于传输协议，不应直接成为用户消息时间线的主要内容。

## 设计

模型基座运行器继续用 Responses SSE 为无进展看门狗续期，并把经过脱敏和裁剪的协议事件保存在 `AgentResult.events` 中，供结果诊断和审计使用。运行日志只写统一的 Agent 语义事件：第一个 `response.created` 转换为一次 `thread.started`；Teamwork 工具执行沿用 `item.started` 与 `item.completed`；最终模型文本转换为 `item.completed / agent_message`；用量转换为 `turn.completed`；失败继续转换为安全的 `error`。

一个 Agent 运行可能因为函数工具循环产生多个 Responses 请求，但只展示一次逻辑会话创建事件，避免把每个模型回合误表示为新的 Agent 会话。最终运行详情继续保存最后一个有效 Response ID，保持现有接口兼容。

前端消息展示对历史运行做兼容：所有 `response.*` 底层协议日志不再生成消息卡片，消息页签数量按归一化后的可见消息计算。历史记录的原始日志仍保存在后端，不执行数据库迁移或删除；CLI 模式和其他系统、工具、文件、命令消息不受影响。
