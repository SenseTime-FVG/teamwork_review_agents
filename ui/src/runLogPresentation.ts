import type { RunLog } from "./types";

export type RunMessageKind = "agent" | "command" | "tool" | "file" | "system" | "error" | "complete";

export type RunMessage = {
  id: number;
  createdAt: number;
  lastCreatedAt: number;
  eventType: string;
  kind: RunMessageKind;
  title: string;
  body: string;
  detail: string;
  raw: string;
  repeatCount: number;
  toolCallId?: string;
  linkedRunId?: string;
  linkedAgentName?: string;
};

type JsonObject = Record<string, unknown>;

const SYSTEM_TITLES: Record<string, string> = {
  "workspace.git.started": "开始 Git 工作区操作",
  "workspace.git.progress": "Git 工作区操作进行中",
  "workspace.git.completed": "Git 工作区操作完成",
  "workspace.git.failed": "Git 工作区操作失败",
  "workspace.git.timed_out": "Git 工作区操作超时",
  "workspace.git.cancelled": "Git 工作区操作已取消",
  "workspace.prepared": "工作区已准备",
  "workspace.prepare.started": "开始准备 Agent 工作区",
  "workspace.prepare.step_started": "开始执行准备步骤",
  "workspace.prepare.output": "准备步骤输出",
  "workspace.prepare.step_completed": "准备步骤已结束",
  "workspace.prepare.completed": "Agent 工作区准备完成",
  "workspace.prepare.failed": "Agent 工作区准备失败",
  "run.home_prepared": "临时 HOME 已准备",
  "run.home_cleaned": "临时 HOME 已清理",
  "run.home_cleanup_failed": "临时 HOME 清理失败",
  "run.started": "Agent 开始运行",
  "thread.started": "Codex 会话已创建",
  "turn.started": "开始处理任务",
  "turn.completed": "本轮处理完成",
  "run.cancel_requested": "已请求取消运行",
  "run.cancelled": "运行已取消",
  "run.timed_out": "运行超过总时限",
  "run.idle_timed_out": "运行因无进展而超时",
  "run.version_mismatch": "Codex CLI 版本不匹配",
  error: "Codex 返回错误",
};

function parsePayload(payload: string): unknown {
  try {
    return JSON.parse(payload);
  } catch {
    return payload;
  }
}

function asObject(value: unknown): JsonObject | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject
    : null;
}

function textValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "";
  return JSON.stringify(value, null, 2);
}

function prettyValue(value: unknown): string {
  if (value === undefined || value === null || value === "") return "";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function itemMessage(log: RunLog, item: JsonObject): RunMessage {
  const itemType = textValue(item.type);
  const status = textValue(item.status);
  const base = {
    id: log.id,
    createdAt: log.created_at,
    lastCreatedAt: log.created_at,
    eventType: log.event_type,
    raw: log.payload,
    repeatCount: 1,
  };
  if (itemType === "agent_message") {
    return { ...base, kind: "agent", title: "Agent", body: textValue(item.text), detail: "" };
  }
  if (itemType === "command_execution") {
    const exitCode = item.exit_code;
    const title = exitCode === undefined || exitCode === null
      ? "运行命令"
      : `命令已结束 · 退出码 ${textValue(exitCode)}`;
    return {
      ...base,
      kind: exitCode === 0 || exitCode === undefined || exitCode === null ? "command" : "error",
      title,
      body: textValue(item.command),
      detail: prettyValue(item.aggregated_output ?? item.output),
    };
  }
  if (itemType === "mcp_tool_call") {
    const toolName = [item.server, item.tool].filter(Boolean).map(textValue).join(" / ");
    const argumentsObject = asObject(item.arguments);
    const linkedRun = asObject(item.linked_run);
    const result = asObject(item.result);
    const invokeAgent = (
      item.server === "teamwork_runtime"
      || item.server === "teamwork_agent_gateway"
    ) && item.tool === "invoke_agent";
    const linkedRunIdValue = linkedRun?.run_id ?? result?.run_id;
    const linkedAgentNameValue = linkedRun?.agent_name ?? argumentsObject?.agent_name;
    return {
      ...base,
      kind: item.error ? "error" : "tool",
      title: item.error ? `工具调用失败${toolName ? ` · ${toolName}` : ""}` : `调用工具${toolName ? ` · ${toolName}` : ""}`,
      body: prettyValue(item.arguments),
      detail: prettyValue(item.error ?? item.result),
      ...(invokeAgent && typeof item.call_id === "string" && item.call_id
        ? { toolCallId: item.call_id }
        : {}),
      ...(invokeAgent && typeof linkedRunIdValue === "string" && linkedRunIdValue
        ? { linkedRunId: linkedRunIdValue }
        : {}),
      ...(invokeAgent && typeof linkedAgentNameValue === "string" && linkedAgentNameValue
        ? { linkedAgentName: linkedAgentNameValue }
        : {}),
    };
  }
  if (itemType === "file_change") {
    return {
      ...base,
      kind: status === "failed" ? "error" : "file",
      title: status === "failed" ? "文件修改失败" : "文件发生修改",
      body: "",
      detail: prettyValue(item.changes),
    };
  }
  if (itemType === "reasoning") {
    return { ...base, kind: "system", title: "Agent 正在分析", body: textValue(item.text), detail: "" };
  }
  if (itemType === "web_search") {
    return { ...base, kind: "tool", title: "搜索网络", body: textValue(item.query), detail: "" };
  }
  return {
    ...base,
    kind: status === "failed" ? "error" : "system",
    title: itemType ? `Codex 项目 · ${itemType}` : "Codex 项目已更新",
    body: "",
    detail: prettyValue(item),
  };
}

function systemMessage(log: RunLog, payload: unknown): RunMessage {
  const object = asObject(payload);
  const title = SYSTEM_TITLES[log.event_type] ?? log.event_type.replaceAll(".", " · ");
  const isError = log.stream === "stderr"
    || /(?:error|failed|timed_out|mismatch|cancelled)/.test(log.event_type);
  let body = "";
  let detail = "";
  if (log.event_type.startsWith("workspace.git.") && object) {
    body = textValue(object.operation);
    detail = [
      object.command ? textValue(object.command) : "",
      `耗时：${textValue(object.elapsed_seconds)} 秒`,
      object.timeout_seconds ? `超时：${textValue(object.timeout_seconds)} 秒` : "",
      object.exit_code !== undefined && object.exit_code !== null ? `退出码：${textValue(object.exit_code)}` : "",
      object.error ? `错误：${textValue(object.error)}` : "",
    ].filter(Boolean).join("\n");
  } else if (log.event_type === "workspace.prepared" && object) {
    body = textValue(object.reason);
    detail = [object.path ? `路径：${textValue(object.path)}` : "", object.mode ? `模式：${textValue(object.mode)}` : ""]
      .filter(Boolean)
      .join("\n");
  } else if (log.event_type.startsWith("workspace.prepare.step_") && object) {
    body = [object.name, object.cwd ? `目录：${textValue(object.cwd)}` : ""]
      .filter(Boolean)
      .join(" · ");
    detail = [
      Array.isArray(object.command) ? JSON.stringify(object.command, null, 2) : "",
      object.status ? `状态：${textValue(object.status)}` : "",
      object.timeout_seconds ? `超时：${textValue(object.timeout_seconds)} 秒` : "",
      object.exit_code !== undefined && object.exit_code !== null ? `退出码：${textValue(object.exit_code)}` : "",
      object.error ? `错误：${textValue(object.error)}` : "",
    ].filter(Boolean).join("\n");
  } else if (
    (log.event_type === "workspace.prepare.started"
      || log.event_type === "workspace.prepare.completed"
      || log.event_type === "workspace.prepare.failed")
    && object
  ) {
    body = object.failed_step
      ? `失败步骤：${textValue(object.failed_step)}`
      : object.steps !== undefined
      ? `准备步骤：${textValue(object.steps)} 个`
      : "";
    detail = [
      object.cache_path ? `仓库缓存：${textValue(object.cache_path)}` : "",
      object.status ? `状态：${textValue(object.status)}` : "",
      object.exit_code !== undefined && object.exit_code !== null ? `退出码：${textValue(object.exit_code)}` : "",
      object.error ? `错误：${textValue(object.error)}` : "",
    ].filter(Boolean).join("\n");
  } else if (log.event_type.startsWith("run.home_") && object) {
    body = object.path ? `路径：${textValue(object.path)}` : "";
    detail = [
      object.mode ? `模式：${textValue(object.mode)}` : "",
      Array.isArray(object.bridges) && object.bridges.length > 0
        ? `桥接：${object.bridges.map(textValue).join("、")}`
        : "",
      object.error ? `错误：${textValue(object.error)}` : "",
    ].filter(Boolean).join("\n");
  } else if (log.event_type === "run.started" && object) {
    body = textValue(object.agent_name);
    detail = object.config_revision ? `配置版本：${textValue(object.config_revision)}` : "";
  } else if (log.event_type === "thread.started" && object) {
    detail = object.thread_id ? `会话：${textValue(object.thread_id)}` : "";
  } else if (log.event_type === "turn.completed" && object) {
    detail = prettyValue(object.usage);
  } else if (typeof payload === "string") {
    body = payload;
  } else if (object) {
    const message = object.message ?? object.error;
    body = textValue(message);
    detail = message ? "" : prettyValue(object);
  }
  return {
    id: log.id,
    createdAt: log.created_at,
    lastCreatedAt: log.created_at,
    eventType: log.event_type,
    kind: isError ? "error" : log.event_type === "turn.completed" ? "complete" : "system",
    title,
    body,
    detail,
    raw: log.payload,
    repeatCount: 1,
  };
}

function toRunMessage(log: RunLog): RunMessage {
  const payload = parsePayload(log.payload);
  const object = asObject(payload);
  if (
    (log.event_type === "item.completed"
      || log.event_type === "item.started"
      || log.event_type === "item.updated")
    && object
  ) {
    const item = asObject(object.item);
    if (item) return itemMessage(log, item);
  }
  return systemMessage(log, payload);
}

export function presentRunLogs(logs: RunLog[]): RunMessage[] {
  const messages: RunMessage[] = [];
  const invokeAgentMessageIndexes = new Map<string, number>();
  for (const log of logs) {
    // Responses SSE 属于模型基座的底层协议，用户时间线只展示统一 Agent 语义事件。
    if (log.event_type.startsWith("response.")) continue;
    const message = toRunMessage(log);
    if (message.toolCallId && message.linkedAgentName) {
      const existingIndex = invokeAgentMessageIndexes.get(message.toolCallId);
      if (existingIndex !== undefined) {
        const existing = messages[existingIndex];
        messages[existingIndex] = {
          ...message,
          id: existing.id,
          createdAt: existing.createdAt,
          linkedRunId: message.linkedRunId ?? existing.linkedRunId,
          linkedAgentName: message.linkedAgentName ?? existing.linkedAgentName,
        };
        continue;
      }
      invokeAgentMessageIndexes.set(message.toolCallId, messages.length);
    }
    const previous = messages.at(-1);
    if (previous && previous.eventType === message.eventType && previous.raw === message.raw) {
      previous.repeatCount += 1;
      previous.lastCreatedAt = message.createdAt;
      continue;
    }
    messages.push(message);
  }
  return messages;
}
