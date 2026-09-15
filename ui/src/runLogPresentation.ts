import type { GitCommandDetail, RunLog } from "./types";

// 仓库、Agent 和 CI 共用安全进度文本；未知阶段不伪造完成百分比。
export function gitProgressText(command: Partial<GitCommandDetail>): string {
  const progress = command.progress;
  if (command.timeout_kind !== "idle") return "";
  const parts = [progress?.label ?? "等待 Git 报告可量化进度"];
  if (progress?.percent != null) parts.push(`当前阶段 ${progress.percent}%`);
  if (progress?.current != null) parts.push(`对象 / 文件 ${progress.current}${progress.total != null ? ` / ${progress.total}` : ""}`);
  if (progress?.received_bytes != null) parts.push(`已传输 ${gitBytesText(progress.received_bytes)}`);
  if (progress?.bytes_per_second != null) parts.push(`最近报告速度 ${gitBytesText(progress.bytes_per_second)}/s`);
  parts.push(`无有效进展 ${command.idle_seconds ?? 0} 秒`);
  return parts.join(" · ");
}

export function gitBytesText(value: number): string {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export type RunMessageKind = "agent" | "command" | "tool" | "file" | "system" | "warning" | "error" | "complete";

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
  "run.runtime_ready": "运行环境检查通过",
  "run.runtime_unavailable": "运行环境检查失败",
  "run.git_https_started": "检查 Windows 沙盒 Git HTTPS",
  "run.git_https_ready": "沙盒 Git HTTPS 检查通过",
  "run.git_https_skipped": "已跳过沙盒 Git HTTPS 检查",
  "run.git_https_failed": "沙盒 Git HTTPS 不可用，已阻断运行",
  "run.git_workspace_trusted": "本轮 Git 工作区信任已配置",
  "run.git_helper_cleanup_failed": "Git 临时 helper 清理失败",
  "run.curl_started": "检测 Windows 沙盒兼容 curl",
  "run.curl_ready": "Windows 沙盒 curl 已就绪",
  "run.curl_unavailable": "兼容 curl 暂不可用，任务可继续",
  "run.curl_warning": "curl 程序已就绪，HTTPS 探测未通过",
  "run.http_tls_unavailable": "HTTP 命令 TLS 失败，可换用兼容命令",
  "thread.started": "Codex 会话已创建",
  "turn.started": "开始处理任务",
  "turn.completed": "本轮处理完成",
  "model.attempt_failed": "模型请求失败",
  "model.fallback": "切换备用模型",
  "model.request_started": "新一轮请求选模",
  "model.quota_exhausted": "额度耗尽，本次运行跳过该模型",
  "model.reasoning_downgraded": "推理强度自动降级",
  "context.compaction_started": "正在压缩执行历史",
  "context.compacted": "上下文已压缩",
  "context.compaction_failed": "上下文无法安全压缩",
  "context.compaction_usage": "历史压缩用量",
  "context.summary_rewrite": "正在进一步收短摘要",
  "context.retry_after_compaction": "上下文超限，压缩后重试",
  "context.tool_output_truncated": "工具结果已精简，完整日志保留",
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
  const title = object?.error_code === "sandbox_git_ownership_mismatch"
    ? "工作区所有权校验失败，已阻断运行"
    : SYSTEM_TITLES[log.event_type] ?? log.event_type.replaceAll(".", " · ");
  const isError = log.stream === "stderr"
    || /(?:error|failed|timed_out|mismatch|cancelled|unavailable)/.test(log.event_type);
  // 能力告警及单个模型额度耗尽不等于整个任务已经终止。
  const isWarning = ["run.curl_unavailable", "run.curl_warning", "run.http_tls_unavailable", "model.quota_exhausted", "context.retry_after_compaction", "context.tool_output_truncated"].includes(log.event_type);
  let body = "";
  let detail = "";
  if (log.event_type.startsWith("context.") && object) {
    // 保守预算不是精确 Token 用量，压缩摘要也不是 Agent 的最终结论。
    body = log.event_type === "context.compacted"
      ? `系统指令、Skill、工具定义和原始任务保持不变；执行历史保守估算由 ${textValue(object.before_estimated_tokens)} 降至 ${textValue(object.after_estimated_tokens)}。`
      : textValue(object.error ?? object.message);
    detail = [
      object.model ? `第 ${textValue(object.request_round)} 轮 · ${textValue(object.provider_id)} / ${textValue(object.model)}` : "",
      object.input_budget ? `输入预算：${textValue(object.input_budget)}${object.window_source ? `；来源：${textValue(object.window_source)}` : ""}` : "",
      object.summary_bytes !== undefined ? `摘要长度：${textValue(object.summary_bytes)} 字节；软目标：${textValue(object.summary_target_bytes)} 字节（非上限）` : "",
      object.after_estimated_tokens !== undefined ? `${object.summary_material_complete === false ? "当前草稿组装保守估算（历史尚未归纳完）" : "完整请求保守估算"}：${textValue(object.after_estimated_tokens)}` : "",
      object.next_summary_request_estimated_tokens != null ? `携带下一片段所需摘要请求预算（最小片段）：${textValue(object.next_summary_request_estimated_tokens)}` : "",
      object.summary_rewrites !== undefined ? `额外收短：${textValue(object.summary_rewrites)} 次` : "",
      object.summary_above_target === true ? "摘要超过软目标，但完整请求在预算内且已缩小，已接受。" : "",
      object.retained_rounds !== undefined ? `保留最近 ${textValue(object.retained_rounds)} 个完整回合；摘要请求 ${textValue(object.summary_requests)} 次` : "",
      object.estimator === "utf8_bytes_conservative" ? "估算方式：UTF-8 序列化字节数及结构余量，不是精确 Token 计数。" : "",
      object.summary ? `历史交接摘要（非最终结果）：\n${textValue(object.summary)}` : "",
      object.call_id ? `工具调用：${textValue(object.call_id)}；原结果：${textValue(object.original_bytes)} 字节` : "",
      object.usage ? prettyValue(object.usage) : "",
      object.error_code ? `错误码：${textValue(object.error_code)}` : "",
      object.retryable === false ? "已停止整轮自动重试，避免重复执行已完成操作。" : "",
    ].filter(Boolean).join("\n");
  } else if (log.event_type === "model.reasoning_downgraded" && object) {
    const next = object.to ? `改用 ${textValue(object.to)}` : "去掉 effort 参数，使用上游默认";
    body = `${textValue(object.from)} 不受上游支持，正在${next}重试。`;
    detail = `${textValue(object.provider_id)} / ${textValue(object.model)}\n${textValue(object.reason)}`;
  } else if (log.event_type === "model.attempt_failed" && object) {
    body = textValue(object.reason);
    detail = `${textValue(object.provider_id)} / ${textValue(object.model)}${object.phase === "compaction" ? " · 历史压缩请求" : ""}`;
  } else if (["model.request_started", "model.quota_exhausted"].includes(log.event_type) && object) {
    body = textValue(object.message);
    detail = [
      `第 ${textValue(object.request_round)} 轮 · ${textValue(object.provider_id)} / ${textValue(object.model)}`,
      textValue(object.reason),
    ].filter(Boolean).join("\n");
  } else if ((log.event_type.startsWith("run.curl_") || log.event_type === "run.http_tls_unavailable") && object) {
    body = textValue(object.message) || "已在当前 Agent 沙盒中检查兼容 curl。";
    detail = [
      object.curl_binary ? `程序：${textValue(object.curl_binary)}` : "",
      object.ssl_backend ? `TLS 后端：${textValue(object.ssl_backend)}` : "",
      object.https_probe ? `HTTPS 探测：${textValue(object.https_probe)}` : "",
      object.reason ? `原因：${textValue(object.reason)}` : "",
      Array.isArray(object.candidates) ? prettyValue(object.candidates) : "",
    ].filter(Boolean).join("\n");
  } else if (log.event_type.startsWith("run.git_") && object) {
    body = textValue(object.error ?? object.reason);
    detail = [
      object.trusted_workspace ? `Git 信任目录：${textValue(object.trusted_workspace)}` : "",
      object.ssl_backend ? `TLS 后端：${textValue(object.ssl_backend)}` : "",
      object.git_binary ? `Git 路径：${textValue(object.git_binary)}` : "",
      object.host ? `远端主机：${textValue(object.host)}` : "",
      object.sha ? `远端 HEAD：${textValue(object.sha)}` : "",
      object.error_code ? `错误码：${textValue(object.error_code)}` : "",
      object.retryable === false ? "确定性错误已停止自动重试，修正后可手动触发。" : "",
    ].filter(Boolean).join("\n");
  } else if (log.event_type.startsWith("run.runtime_") && object) {
    body = object.error ? textValue(object.error) : "已在创建工作区前完成运行环境检查。";
    detail = [
      object.configured_command ? `配置命令：${textValue(object.configured_command)}` : "",
      object.resolved_path ? `实际路径：${textValue(object.resolved_path)}` : "",
      object.discovery_source ? `发现来源：${textValue(object.discovery_source)}` : "",
      object.error_code ? `错误码：${textValue(object.error_code)}` : "",
      object.retryable === false ? "自动重试已停止，修正后可手动触发。" : "",
    ].filter(Boolean).join("\n");
  } else if (log.event_type.startsWith("workspace.git.") && object) {
    body = textValue(object.operation);
    detail = [
      object.command ? textValue(object.command) : "",
      `总耗时：${textValue(object.elapsed_seconds)} 秒`,
      object.timeout_seconds ? `${object.timeout_kind === "idle" ? "无进展超时" : "超时"}：${textValue(object.timeout_seconds)} 秒` : "",
      gitProgressText(object as Partial<GitCommandDetail>),
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
    kind: isWarning ? "warning" : isError ? "error" : log.event_type === "turn.completed" ? "complete" : "system",
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
