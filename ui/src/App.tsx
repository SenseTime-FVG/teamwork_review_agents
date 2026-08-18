import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  getToken,
  setToken as persistToken,
  streamRunLogs,
  uploadPromptFile,
  uploadSkillDirectory,
} from "./api";
import type { ManagedPromptFile, ManagedSkillDirectory } from "./api";
import { MarkdownMessage, RunMessageFeed } from "./RunMessageFeed";
import type {
  Agent,
  ChangeRequestRecord,
  CodexAccountStatus,
  CodexLoginSession,
  CodexRuntimeConfig,
  CodexRuntimeOptions,
  ConfigDocument,
  EnvironmentMap,
  EnvironmentVariable,
  EventRecord,
  Repository,
  Rule,
  RunDetail,
  RunLog,
  RunSummary,
  RuntimeStatus,
  Skill,
} from "./types";

type Tab = "overview" | "repositories" | "environment" | "skills" | "agents" | "rules" | "runtime" | "runs";

const EMPTY_STATUS: RuntimeStatus = {
  paused: false,
  running_cycle: false,
  config_revision: "",
  stats: { runs: {}, events: {}, change_requests: {} },
};

const EMPTY_CODEX_OPTIONS: CodexRuntimeOptions = {
  models: [],
  inherited_model: {
    value: null,
    source: "builtin",
    label: "继承 Codex CLI / 账号默认（未配置固定模型）",
  },
  user_model: null,
  user_config_path: "~/.codex/config.toml",
  codex_home: "~/.codex",
  binary: {},
  model_cache: { path: "~/.codex/models_cache.json" },
  user_mcp_servers: [],
};

const REASONING_LEVELS = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"];

function normalizeDocument(value: Partial<ConfigDocument>): ConfigDocument {
  const scannerInput = { ...(value.scanner ?? {}) };
  const legacyPageSize = Number(scannerInput.page_size ?? 50);
  const legacyMaxPages = Number(scannerInput.max_pages ?? 2);
  const maxItems = Number(
    scannerInput.max_items_per_repository
      ?? legacyPageSize * legacyMaxPages,
  );
  delete scannerInput.max_pages;
  delete scannerInput.page_size;
  const runtimeInput = { ...(value.runtime ?? {}) };
  const codexInput = { ...(runtimeInput.codex ?? {}) };
  return {
    database: value.database ?? { path: "../data/teamwork-review-agents.db" },
    scanner: {
      interval_seconds: 300,
      max_items_per_repository: maxItems,
      emit_initial_events: false,
      ...scannerInput,
    },
    runtime: {
      max_concurrent_agents: 4,
      lock_timeout_seconds: 300,
      lock_ttl_seconds: 120,
      max_sub_agent_depth: 2,
      max_agent_runs_per_root: 8,
      event_retry_count: 2,
      worktree_retention_days: 7,
      codex_binary: "codex",
      inherit_user_mcp_servers: false,
      allowed_user_mcp_servers: [],
      agent_idle_timeout_seconds: 300,
      ...runtimeInput,
      codex: {
        fast_mode: "inherit",
        extra_config: {},
        ...codexInput,
      },
    },
    web: {
      host: "127.0.0.1",
      port: 8080,
      config_poll_seconds: 2,
      log_retention_days: 30,
      ...(value.web ?? {}),
    },
    environment: { global: value.environment?.global ?? {} },
    providers: value.providers ?? {},
    repositories: value.repositories ?? [],
    skills: value.skills ?? {},
    agents: value.agents ?? {},
    rules: value.rules ?? [],
  };
}

function timeText(timestamp?: number | null): string {
  return timestamp ? new Date(timestamp * 1000).toLocaleString("zh-CN") : "—";
}

function dateTimeText(value?: string | null): string {
  return value ? new Date(value).toLocaleString("zh-CN") : "—";
}

function shortRevision(revision?: string): string {
  return revision ? revision.slice(0, 9) : "—";
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "执行中",
    completed: "已完成",
    failed: "失败",
    timed_out: "超时",
    cancelled: "已取消",
    pending: "待处理",
    processing: "处理中",
    unmatched: "未触发",
    triggered: "已触发",
    opened: "打开",
    closed: "已关闭",
    merged: "已合并",
  };
  return labels[status] ?? status;
}

function workspaceStatusLabel(status?: string | null): string {
  const labels: Record<string, string> = {
    active: "使用中",
    removed: "已清理",
    retained: "待清理",
    inherited: "继承父工作区",
    "not-created": "未创建",
  };
  return status ? labels[status] ?? status : "未知";
}

function Field(props: {
  label: string;
  value: string | number;
  onChange: (value: string) => void;
  type?: string;
  placeholder?: string;
  help?: string;
  disabled?: boolean;
}) {
  return (
    <label className="field">
      <span>{props.label}</span>
      <input
        type={props.type ?? "text"}
        value={props.value}
        placeholder={props.placeholder}
        disabled={props.disabled}
        onChange={(event) => props.onChange(event.target.value)}
      />
      {props.help && <small>{props.help}</small>}
    </label>
  );
}

function SelectField(props: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: Array<{ value: string; label: string }>;
  help?: string;
}) {
  return (
    <label className="field">
      <span>{props.label}</span>
      <select value={props.value} onChange={(event) => props.onChange(event.target.value)}>
        {props.options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
      {props.help && <small>{props.help}</small>}
    </label>
  );
}

function ModelField(props: {
  id: string;
  label: string;
  value: string;
  placeholder: string;
  models: CodexRuntimeOptions["models"];
  onChange: (value: string) => void;
  help?: string;
}) {
  return (
    <label className="field">
      <span>{props.label}</span>
      <input
        list={props.id}
        value={props.value}
        placeholder={props.placeholder}
        onChange={(event) => props.onChange(event.target.value)}
      />
      <datalist id={props.id}>
        {props.models.map((model) => <option key={model.slug} value={model.slug}>{model.display_name}</option>)}
      </datalist>
      {props.help && <small>{props.help}</small>}
    </label>
  );
}

function CommitField(props: {
  label: string;
  value: string;
  onCommit: (value: string) => boolean;
  className?: string;
}) {
  const [draft, setDraft] = useState(props.value);
  useEffect(() => setDraft(props.value), [props.value]);
  return (
    <label className={`field ${props.className ?? ""}`}>
      <span>{props.label}</span>
      <input
        className="mono"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => {
          if (!props.onCommit(draft.trim())) setDraft(props.value);
        }}
      />
    </label>
  );
}

function Toggle(props: { label: string; checked: boolean; disabled?: boolean; onChange: (value: boolean) => void }) {
  return (
    <label className={`toggle-row ${props.disabled ? "disabled" : ""}`}>
      <button
        type="button"
        role="switch"
        aria-checked={props.checked}
        disabled={props.disabled}
        className={`toggle ${props.checked ? "active" : ""}`}
        onClick={() => props.onChange(!props.checked)}
      >
        <span />
      </button>
      {props.label}
    </label>
  );
}

function NetworkDomainsField(props: {
  value: string[];
  onChange: (value: string[]) => void;
}) {
  const serialized = props.value.join("\n");
  const [draft, setDraft] = useState(serialized);
  useEffect(() => setDraft(serialized), [serialized]);
  function apply() {
    const domains = Array.from(new Set(
      draft
        .split(/[\n,]/)
        .map((value) => value.trim().toLowerCase())
        .filter(Boolean),
    ));
    props.onChange(domains);
    setDraft(domains.join("\n"));
  }
  return (
    <label className="field network-domain-field">
      <span>可选域名白名单</span>
      <textarea
        className="mono"
        rows={4}
        value={draft}
        placeholder={"api.github.com\n*.github.com\n**.example.com"}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={apply}
      />
      <small>每行或逗号分隔一个域名；留空表示不限制目标域名。</small>
    </label>
  );
}

function MultiSelect(props: {
  label: string;
  values: string[];
  options: string[];
  onChange: (values: string[]) => void;
}) {
  function toggle(option: string) {
    props.onChange(
      props.values.includes(option)
        ? props.values.filter((value) => value !== option)
        : [...props.values, option],
    );
  }

  return (
    <div className="multi-choice-field">
      <div className="multi-choice-head">
        <span>{props.label}</span>
        <small>已选择 {props.values.length} 个</small>
      </div>
      {props.options.length === 0 ? (
        <div className="multi-choice-empty">暂无可选项</div>
      ) : (
        <div className="multi-choice-list">
          {props.options.map((option) => {
            const checked = props.values.includes(option);
            return (
              <label className={`multi-choice-option ${checked ? "selected" : ""}`} key={option}>
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggle(option)}
                />
                <span>{option}</span>
              </label>
            );
          })}
        </div>
      )}
      <small className="multi-choice-help">直接点击选项即可多选或取消，不需要按 Command / Ctrl。</small>
    </div>
  );
}

function ChoiceCards(props: {
  title: string;
  description: string;
  values: string[];
  options: Array<{ value: string; label: string; description: string }>;
  emptyText?: string;
  onChange: (values: string[]) => void;
}) {
  return (
    <div className="choice-section">
      <div className="choice-title"><strong>{props.title}</strong><p>{props.description}</p></div>
      {props.options.length === 0 ? (
        <div className="choice-empty">{props.emptyText ?? "暂无可选项"}</div>
      ) : (
        <div className="choice-list">
          {props.options.map((option) => {
            const checked = props.values.includes(option.value);
            return (
              <label className={`choice-card ${checked ? "selected" : ""}`} key={option.value}>
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => props.onChange(
                    checked
                      ? props.values.filter((value) => value !== option.value)
                      : [...props.values, option.value],
                  )}
                />
                <span><strong>{option.label}</strong><small>{option.description}</small></span>
              </label>
            );
          })}
        </div>
      )}
    </div>
  );
}

function PromptFilePicker(props: {
  value: string;
  onChange: (value: string) => void;
}) {
  const [files, setFiles] = useState<ManagedPromptFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const loadFiles = useCallback(async () => {
    try {
      setFiles(await api<ManagedPromptFile[]>("/api/prompt-files"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取 Prompt 文件失败");
    }
  }, []);

  useEffect(() => { void loadFiles(); }, [loadFiles]);

  async function importFile(file?: File) {
    if (!file) return;
    setUploading(true);
    setError("");
    setMessage("");
    try {
      const imported = await uploadPromptFile(file);
      props.onChange(imported.path);
      setMessage(`已导入 ${imported.name}`);
      await loadFiles();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "导入 Prompt 文件失败");
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="prompt-file-picker">
      <Field
        label="Prompt 文件路径"
        value={props.value}
        onChange={props.onChange}
        placeholder="./prompts/reviewer.md"
        help="相对路径以 config.yaml 所在目录为基准"
      />
      <div className="prompt-file-actions">
        <select
          value=""
          onChange={(event) => {
            if (event.target.value) props.onChange(event.target.value);
          }}
        >
          <option value="">选择已有 Prompt…</option>
          {files.map((file) => (
            <option key={file.path} value={file.path}>{file.name}</option>
          ))}
        </select>
        <label className={`button secondary file-button ${uploading ? "disabled" : ""}`}>
          {uploading ? "正在导入…" : "从电脑选择并导入"}
          <input
            type="file"
            accept=".md,.txt,text/markdown,text/plain"
            disabled={uploading}
            onChange={(event) => {
              void importFile(event.target.files?.[0]);
              event.target.value = "";
            }}
          />
        </label>
        <button className="button secondary" type="button" onClick={() => { void loadFiles(); }}>刷新列表</button>
      </div>
      <p className="field-help">选择的文件会复制到配置目录的 `./prompts/`，支持 UTF-8 编码的 `.md` 和 `.txt`，最大 1 MiB。</p>
      {message && <p className="inline-message success-text">{message}</p>}
      {error && <p className="inline-message error-text">{error}</p>}
    </div>
  );
}

function normalizeVariable(value: EnvironmentMap[string]): EnvironmentVariable {
  if (value && typeof value === "object") {
    return {
      value: value.value,
      from_system: value.from_system,
      secret: value.secret ?? false,
      expose_to_prompt: value.expose_to_prompt ?? !(value.secret ?? false),
      expose_to_process: value.expose_to_process ?? true,
    };
  }
  return {
    value: value == null ? "" : String(value),
    secret: false,
    expose_to_prompt: true,
    expose_to_process: true,
  };
}

function providerCredentialNames(document: ConfigDocument): Set<string> {
  return new Set(
    Object.values(document.providers)
      .map((provider) => String(provider.token_env ?? "").trim())
      .filter(Boolean),
  );
}

function protectProviderVariable(
  name: string,
  variable: EnvironmentVariable,
  protectedNames: ReadonlySet<string>,
): EnvironmentVariable {
  if (!protectedNames.has(name)) return variable;
  return {
    ...variable,
    secret: true,
    expose_to_prompt: false,
    expose_to_process: false,
  };
}

function EnvironmentEditor(props: {
  title: string;
  value: EnvironmentMap;
  onChange: (value: EnvironmentMap) => void;
  compact?: boolean;
  protectedNames?: ReadonlySet<string>;
}) {
  const entries = Object.entries(props.value);
  const protectedNames = props.protectedNames ?? new Set<string>();

  function update(name: string, nextName: string, variable: EnvironmentVariable) {
    const next = { ...props.value };
    delete next[name];
    if (nextName) {
      next[nextName] = protectProviderVariable(nextName, variable, protectedNames);
    }
    props.onChange(next);
  }

  return (
    <section className={props.compact ? "nested-section" : "section-card"}>
      <div className="section-title-row">
        <div>
          <h2>{props.title}</h2>
          <p>优先级由全局到仓库再到 Agent；同名变量由更具体的一层覆盖。</p>
        </div>
        <button
          type="button"
          className="button secondary"
          onClick={() => props.onChange({ ...props.value, NEW_VARIABLE: "" })}
        >
          + 添加变量
        </button>
      </div>
      {entries.length === 0 && <div className="empty">还没有配置环境变量</div>}
      <div className="env-list">
        {entries.map(([name, raw]) => {
          const isProviderCredential = protectedNames.has(name);
          const variable = protectProviderVariable(
            name,
            normalizeVariable(raw),
            protectedNames,
          );
          const source = variable.from_system !== undefined ? "system" : "value";
          return (
            <div className={`env-row ${isProviderCredential ? "provider-credential" : ""}`} key={name}>
              <div className="env-name-cell">
                <CommitField
                  label=""
                  value={name}
                  className="env-name-field"
                  onCommit={(nextName) => {
                    if (!/^[A-Za-z][A-Za-z0-9_]*$/.test(nextName)) return false;
                    if (nextName !== name && Object.hasOwn(props.value, nextName)) return false;
                    update(name, nextName, variable);
                    return true;
                  }}
                />
                {isProviderCredential && <span className="credential-badge">Provider 凭据</span>}
              </div>
              <select
                value={source}
                aria-label="变量来源"
                onChange={(event) => {
                  const fromSystem = event.target.value === "system";
                  update(name, name, {
                    ...variable,
                    value: fromSystem ? undefined : "",
                    from_system: fromSystem ? name : undefined,
                  });
                }}
              >
                <option value="value">固定值</option>
                <option value="system">宿主机环境</option>
              </select>
              <input
                type={variable.secret && source === "value" ? "password" : "text"}
                value={source === "system" ? variable.from_system ?? "" : variable.value ?? ""}
                placeholder={source === "system" ? "宿主机变量名" : "变量值"}
                onChange={(event) =>
                  update(name, name, {
                    ...variable,
                    ...(source === "system"
                      ? { from_system: event.target.value, value: undefined }
                      : { value: event.target.value, from_system: undefined }),
                  })
                }
              />
              <label className="mini-check">
                <input
                  type="checkbox"
                  checked={variable.secret ?? false}
                  disabled={isProviderCredential}
                  onChange={(event) =>
                    update(name, name, {
                      ...variable,
                      secret: event.target.checked,
                      expose_to_prompt: event.target.checked ? false : variable.expose_to_prompt,
                    })
                  }
                />
                Secret
              </label>
              <label className="mini-check">
                <input
                  type="checkbox"
                  checked={variable.expose_to_prompt ?? false}
                  disabled={isProviderCredential}
                  onChange={(event) => update(name, name, { ...variable, expose_to_prompt: event.target.checked })}
                />
                Prompt
              </label>
              <label className="mini-check">
                <input
                  type="checkbox"
                  checked={variable.expose_to_process ?? true}
                  disabled={isProviderCredential}
                  onChange={(event) => update(name, name, { ...variable, expose_to_process: event.target.checked })}
                />
                进程
              </label>
              <button
                type="button"
                className="icon-button danger"
                aria-label={`删除 ${name}`}
                onClick={() => {
                  const next = { ...props.value };
                  delete next[name];
                  props.onChange(next);
                }}
              >
                ×
              </button>
            </div>
          );
        })}
      </div>
      <p className="section-note">Secret 默认不会进入 Prompt，日志与配置历史中会显示为 ********。</p>
      {entries.some(([name]) => protectedNames.has(name)) && (
        <p className="section-note credential-note">Provider 凭据只供后台扫描器访问平台 API，始终不会进入 Prompt 或 Codex 进程。</p>
      )}
    </section>
  );
}

function Overview(props: {
  status: RuntimeStatus;
  events: EventRecord[];
  changeRequests: ChangeRequestRecord[];
  emittingKey: string;
  onAction: (action: "scan" | "pause" | "resume") => void;
  onEmitDiscovered: (item: ChangeRequestRecord) => void;
}) {
  const runTotal = Object.values(props.status.stats.runs).reduce((sum, value) => sum + value, 0);
  const eventTotal = Object.values(props.status.stats.events).reduce((sum, value) => sum + value, 0);
  const changeRequestTotal = props.status.stats.change_requests.total ?? 0;
  const pendingEvents = (props.status.stats.events.pending ?? 0) + (props.status.stats.events.processing ?? 0);
  return (
    <div className="page-stack">
      <section className="hero-card">
        <div>
          <span className="eyebrow">后台调度器</span>
          <h1>{props.status.paused ? "扫描已暂停" : props.status.running_cycle ? "正在扫描与调度" : "服务运行正常"}</h1>
          <p>配置版本 {shortRevision(props.status.config_revision)} · 最近完成 {timeText(props.status.last_finished_at)}</p>
        </div>
        <div className="button-group">
          <button className="button primary" onClick={() => props.onAction("scan")}>立即扫描</button>
          <button
            className="button secondary"
            onClick={() => props.onAction(props.status.paused ? "resume" : "pause")}
          >
            {props.status.paused ? "恢复" : "暂停"}
          </button>
        </div>
      </section>
      {(props.status.config_error || props.status.last_error) && (
        <div className="alert error">{props.status.config_error ?? props.status.last_error}</div>
      )}
      <div className="metric-grid">
        <div className="metric-card"><span>已扫描 MR / PR</span><strong>{changeRequestTotal}</strong><small>{props.status.stats.change_requests.opened ?? 0} 个处于打开状态</small></div>
        <div className="metric-card"><span>变化事件</span><strong>{eventTotal}</strong><small>{pendingEvents} 个待处理</small></div>
        <div className="metric-card"><span>Agent 运行</span><strong>{runTotal}</strong><small>{props.status.stats.runs.running ?? 0} 个执行中 · {props.status.stats.runs.queued ?? 0} 个排队中</small></div>
        <div className="metric-card"><span>最近周期</span><strong>{props.status.running_cycle ? "进行中" : "已结束"}</strong><small>{timeText(props.status.last_started_at)}</small></div>
      </div>
      <section className="section-card">
        <div className="section-title-row">
          <div><h2>已扫描 MR / PR</h2><p>扫描器在 SQLite 中保存的最新快照；这里的数量与变化事件分开统计。</p></div>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>MR / PR</th><th>仓库</th><th>状态</th><th>远端更新</th><th>最近扫描</th><th>首次事件</th></tr></thead>
            <tbody>
              {props.changeRequests.map((item) => (
                <tr key={item.snapshot_key}>
                  <td>
                    <a className="change-request-link" href={item.web_url} target="_blank" rel="noreferrer">
                      <strong>#{item.number} {item.title}</strong>
                      <small>{item.source_branch} → {item.target_branch}</small>
                    </a>
                  </td>
                  <td>{item.repository_id}</td>
                  <td><StatusPill value={item.state} /></td>
                  <td>{dateTimeText(item.updated_at)}</td>
                  <td>{timeText(item.scanned_at)}</td>
                  <td>
                    {item.discovered_event_emitted ? (
                      <span className="event-emitted">已产生</span>
                    ) : (
                      <button
                        className="button secondary compact"
                        disabled={props.emittingKey === item.snapshot_key}
                        onClick={() => props.onEmitDiscovered(item)}
                      >
                        {props.emittingKey === item.snapshot_key ? "补发中…" : "补发首次事件"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {props.changeRequests.length === 0 && <div className="empty">尚未扫描到 MR / PR，请确认仓库已启用并执行扫描。</div>}
        </div>
      </section>
      <section className="section-card">
        <div className="section-title-row"><div><h2>最近变化事件</h2><p>新发现、提交、状态、标签等变化产生的语义事件，不代表 PR 总数。</p></div></div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>事件</th><th>仓库</th><th>编号</th><th>事件状态</th><th>Agent</th><th>时间</th></tr></thead>
            <tbody>
              {props.events.map((event) => (
                <tr key={event.event_id}>
                  <td className="mono">{event.event_type}</td>
                  <td>{event.repository_id}</td>
                  <td>#{event.number}</td>
                  <td><EventStatusPill event={event} /></td>
                  <td><EventAgentProgress event={event} /></td>
                  <td>{timeText(event.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {props.events.length === 0 && <div className="empty">尚未产生事件</div>}
        </div>
      </section>
    </div>
  );
}

function GlobalEnvironment(props: {
  document: ConfigDocument;
  onChange: (document: ConfigDocument) => void;
}) {
  const protectedNames = providerCredentialNames(props.document);
  function patchSection(section: "scanner" | "runtime" | "web", key: string, value: unknown) {
    props.onChange({
      ...props.document,
      [section]: { ...props.document[section], [key]: value },
    });
  }
  return (
    <div className="page-stack">
      <EnvironmentEditor
        title="全局环境变量"
        value={props.document.environment.global}
        protectedNames={protectedNames}
        onChange={(global) => props.onChange({ ...props.document, environment: { global } })}
      />
      <section className="section-card">
        <div className="section-title-row"><div><h2>后台与扫描设置</h2><p>这些值保存后由常驻进程热加载。</p></div></div>
        <div className="form-grid three">
          <Field label="扫描间隔（分钟）" type="number" value={Number(props.document.scanner.interval_seconds) / 60} onChange={(value) => patchSection("scanner", "interval_seconds", Math.round(Number(value) * 60))} help="默认每 5 分钟扫描一次；服务启动后会先立即扫描" />
          <Field label="每个仓库每轮最多扫描 MR / PR" type="number" value={Number(props.document.scanner.max_items_per_repository ?? 100)} onChange={(value) => patchSection("scanner", "max_items_per_repository", Number(value))} help="默认 100 条；后台自动分页，并在到达上次成功扫描时间后提前停止" />
          <Field label="配置检测（秒）" type="number" value={Number(props.document.web.config_poll_seconds)} onChange={(value) => patchSection("web", "config_poll_seconds", Number(value))} />
          <Field label="日志保留（天）" type="number" value={Number(props.document.web.log_retention_days)} onChange={(value) => patchSection("web", "log_retention_days", Number(value))} />
          <Field label="最大并行 Agent" type="number" value={Number(props.document.runtime.max_concurrent_agents)} onChange={(value) => patchSection("runtime", "max_concurrent_agents", Number(value))} />
          <Field label="异常工作区保留（天）" type="number" value={Number(props.document.runtime.worktree_retention_days ?? 7)} onChange={(value) => patchSection("runtime", "worktree_retention_days", Number(value))} help="失败、未提交文件或未推送提交默认保留 7 天；到期后会在下次准备同仓库时清理" />
          <Field label="监听地址" value={String(props.document.web.host)} onChange={(value) => patchSection("web", "host", value)} />
          <Field label="端口" type="number" value={Number(props.document.web.port)} onChange={(value) => patchSection("web", "port", Number(value))} />
          <Field label="管理员 Token 环境变量" value={String(props.document.web.admin_token_env ?? "")} onChange={(value) => patchSection("web", "admin_token_env", value || undefined)} help="非本机监听时必填" />
        </div>
        <div className="toggle-grid">
          <Toggle label="首次建立快照时额外记录 discovered" checked={Boolean(props.document.scanner.emit_initial_events)} onChange={(value) => patchSection("scanner", "emit_initial_events", value)} />
          <small>只控制 discovered；扫描周期内真实发生的 opened、closed、reopened 等事件始终会记录。</small>
        </div>
      </section>
    </div>
  );
}

function effectiveInheritedModel(
  document: ConfigDocument,
  options: CodexRuntimeOptions,
): { value?: string | null; label: string } {
  const runtimeModel = document.runtime.codex?.model?.trim();
  if (runtimeModel) {
    return {
      value: runtimeModel,
      label: `继承 Teamwork 运行时默认（${runtimeModel}）`,
    };
  }
  if (options.user_model) {
    return {
      value: options.user_model,
      label: `继承 Codex 用户配置（${options.user_model}）`,
    };
  }
  return {
    value: null,
    label: "继承 Codex CLI / 账号默认（未配置固定模型）",
  };
}

function reasoningLevels(
  options: CodexRuntimeOptions,
  model?: string | null,
  current?: string,
): string[] {
  const modelEntry = options.models.find((item) => item.slug === model);
  return Array.from(new Set([
    ...(modelEntry?.supported_reasoning_levels ?? REASONING_LEVELS),
    ...(current ? [current] : []),
  ]));
}

function CodexRuntimeEditor(props: {
  document: ConfigDocument;
  options: CodexRuntimeOptions;
  onChange: (document: ConfigDocument) => void;
}) {
  const codex = props.document.runtime.codex ?? {};
  const inherited = effectiveInheritedModel(props.document, props.options);
  const selectedModel = codex.model || props.options.user_model;

  function patchRuntime(patch: Record<string, unknown>) {
    props.onChange({
      ...props.document,
      runtime: { ...props.document.runtime, ...patch },
    });
  }

  function patchCodex(patch: Partial<CodexRuntimeConfig>) {
    patchRuntime({ codex: { ...codex, ...patch } });
  }

  return (
    <div className="page-stack">
      <section className="section-card">
        <div className="section-title-row">
          <div>
            <h2>Codex CLI 默认参数</h2>
            <p>只影响 Teamwork 发起的 Codex 进程，不会修改你的 Codex 用户配置文件。</p>
          </div>
        </div>
        <div className="agent-workspace-note">
          <strong>当前模型来源</strong>
          <span>{inherited.label}。Agent 可以单独覆盖；仓库中的 `.codex/config.toml` 仅在没有更高优先级覆盖时参与 Codex 原生合并。</span>
        </div>
        <div className="agent-workspace-note">
          <strong>实际后台 CLI</strong>
          <span>
            {props.options.binary.resolved_path ?? String(props.document.runtime.codex_binary ?? "codex")}
            {props.options.binary.version ? ` · ${props.options.binary.version}` : " · 版本无法识别"}
            {` · CODEX_HOME ${props.options.codex_home}`}
          </span>
        </div>
        {props.options.version_warning && <div className="alert error">{props.options.version_warning}</div>}
        <div className="form-grid three runtime-config-grid">
          <Field
            label="Codex CLI 命令"
            value={String(props.document.runtime.codex_binary ?? "codex")}
            onChange={(codex_binary) => patchRuntime({ codex_binary: codex_binary || "codex" })}
            help="可以填写命令名或服务端绝对路径"
          />
          <Field
            label="后台 Codex Home（可选）"
            value={String(props.document.runtime.codex_home ?? "")}
            placeholder="留空继承 CODEX_HOME 或 ~/.codex"
            onChange={(codex_home) => patchRuntime({ codex_home: codex_home || undefined })}
            help="配置独立目录可隔离模型缓存；需要在该目录下单独完成 Codex 登录"
          />
          <Field
            label="期望 CLI 版本（可选）"
            value={String(props.document.runtime.expected_codex_version ?? "")}
            placeholder={props.options.binary.version ?? "例如 0.146.0"}
            onChange={(expected_codex_version) => patchRuntime({ expected_codex_version: expected_codex_version || undefined })}
            help="填写后版本不一致会阻止 Agent 启动"
          />
          <Field
            label="默认无进展超时（秒）"
            type="number"
            value={Number(props.document.runtime.agent_idle_timeout_seconds ?? 300)}
            onChange={(value) => patchRuntime({ agent_idle_timeout_seconds: Number(value) })}
            help="只由 stdout / JSONL 续期，重复 stderr 不会延长"
          />
          <ModelField
            id="runtime-codex-models"
            label="默认模型"
            value={codex.model ?? ""}
            placeholder="继承 Codex 配置或账号默认"
            models={props.options.models}
            onChange={(model) => patchCodex({ model: model || undefined })}
            help={props.options.catalog_error ? "无法读取本机模型目录，仍可手工填写模型 ID" : "候选项来自当前服务使用的 Codex CLI"}
          />
          <SelectField
            label="默认推理强度"
            value={codex.model_reasoning_effort ?? ""}
            onChange={(value) => patchCodex({ model_reasoning_effort: value || undefined })}
            options={[
              { value: "", label: "继承 Codex 配置 / 模型默认" },
              ...reasoningLevels(props.options, selectedModel, codex.model_reasoning_effort).map((value) => ({ value, label: value })),
            ]}
            help="不同模型支持的强度可能不同"
          />
          <SelectField
            label="快速模式"
            value={codex.fast_mode ?? "inherit"}
            onChange={(value) => patchCodex({ fast_mode: value as CodexRuntimeConfig["fast_mode"] })}
            options={[
              { value: "inherit", label: "继承 Codex 配置" },
              { value: "standard", label: "标准模式" },
              { value: "fast", label: "快速模式" },
            ]}
            help="快速模式的可用性和用量倍率由当前模型与账号决定"
          />
          <SelectField
            label="输出详细度"
            value={codex.model_verbosity ?? ""}
            onChange={(value) => patchCodex({ model_verbosity: value ? value as CodexRuntimeConfig["model_verbosity"] : undefined })}
            options={[
              { value: "", label: "继承 Codex 配置" },
              { value: "low", label: "低" },
              { value: "medium", label: "中" },
              { value: "high", label: "高" },
            ]}
          />
          <SelectField
            label="交互风格"
            value={codex.personality ?? ""}
            onChange={(value) => patchCodex({ personality: value ? value as CodexRuntimeConfig["personality"] : undefined })}
            options={[
              { value: "", label: "继承 Codex 配置" },
              { value: "none", label: "无预设" },
              { value: "friendly", label: "友好" },
              { value: "pragmatic", label: "务实" },
            ]}
          />
          <SelectField
            label="联网搜索"
            value={codex.web_search ?? ""}
            onChange={(value) => patchCodex({ web_search: value ? value as CodexRuntimeConfig["web_search"] : undefined })}
            options={[
              { value: "", label: "继承 Codex 配置" },
              { value: "disabled", label: "禁用" },
              { value: "cached", label: "缓存搜索" },
              { value: "live", label: "实时搜索" },
            ]}
          />
        </div>
      </section>
      <section className="section-card">
        <div className="section-title-row">
          <div>
            <h2>后台 MCP 能力隔离</h2>
            <p>默认关闭用户 Codex 配置中的 MCP，只保留 Teamwork sub-agent 网关；平台操作优先使用 MR / PR 输入、API、gh / glab 和本地工作区。</p>
          </div>
        </div>
        <div className="toggle-grid">
          <Toggle
            label="继承全部用户 MCP（高风险）"
            checked={Boolean(props.document.runtime.inherit_user_mcp_servers)}
            onChange={(inherit_user_mcp_servers) => patchRuntime({ inherit_user_mcp_servers })}
          />
          <small>开启后，浏览器、Computer Use、REPL 等用户 MCP 也可能被后台 Agent 调用。</small>
        </div>
        {!props.document.runtime.inherit_user_mcp_servers && (
          <MultiSelect
            label="允许继承的用户 MCP"
            values={props.document.runtime.allowed_user_mcp_servers ?? []}
            options={props.options.user_mcp_servers}
            onChange={(allowed_user_mcp_servers) => patchRuntime({ allowed_user_mcp_servers })}
          />
        )}
      </section>
      <section className="section-card">
        <div className="section-title-row">
          <div>
            <h2>高级 Codex 配置</h2>
            <p>使用 Codex 原生点号键。值按 JSON 编写；结构化字段、安全策略、MCP 和 Skill 不能在这里覆盖。</p>
          </div>
        </div>
        <JsonEditor
          label="额外配置（JSON）"
          value={codex.extra_config ?? {}}
          help={'示例：{ "features.some_flag": true, "history.max_bytes": 1048576 }'}
          onChange={(extra_config) => patchCodex({
            extra_config: extra_config as CodexRuntimeConfig["extra_config"],
          })}
        />
      </section>
    </div>
  );
}

function CodexQuotaWindow(props: {
  label: string;
  value?: { usedPercent?: number; windowDurationMins?: number; resetsAt?: number };
}) {
  if (!props.value) return null;
  const usedPercent = Math.max(0, Math.min(100, Number(props.value.usedPercent ?? 0)));
  const duration = props.value.windowDurationMins
    ? `${props.value.windowDurationMins} 分钟窗口`
    : "额度窗口";
  return (
    <div className="codex-quota-window">
      <div><span>{props.label} · {duration}</span><strong>{usedPercent.toFixed(0)}% 已使用</strong></div>
      <div className="codex-quota-track"><span style={{ width: `${usedPercent}%` }} /></div>
      {props.value.resetsAt && <small>重置时间：{timeText(props.value.resetsAt)}</small>}
    </div>
  );
}

function CodexAccountCard(props: {
  configuredHome?: string;
  homeHasUnsavedChange: boolean;
}) {
  const [account, setAccount] = useState<CodexAccountStatus | null>(null);
  const [login, setLogin] = useState<CodexLoginSession | null>(null);
  const [loading, setLoading] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    if (!props.configuredHome) {
      setAccount(null);
      setError("");
      return;
    }
    setLoading(true);
    setError("");
    try {
      setAccount(await api<CodexAccountStatus>("/api/codex/account"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取 Codex 账户失败");
    } finally {
      setLoading(false);
    }
  }, [props.configuredHome]);

  useEffect(() => {
    setLogin(null);
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!login || login.status !== "pending") return undefined;
    let stopped = false;
    let timer = 0;
    const poll = async () => {
      try {
        const next = await api<CodexLoginSession>(`/api/codex/login/${login.session_id}`);
        if (stopped) return;
        setLogin(next);
        if (next.status === "pending") {
          timer = window.setTimeout(() => { void poll(); }, 1000);
        } else if (next.status === "completed") {
          await refresh();
        }
      } catch (reason) {
        if (!stopped) {
          setError(reason instanceof Error ? reason.message : "读取 Codex 登录状态失败");
        }
      }
    };
    timer = window.setTimeout(() => { void poll(); }, 700);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [login?.session_id, login?.status, refresh]);

  async function startLogin() {
    if (props.homeHasUnsavedChange) return;
    setWorking(true);
    setError("");
    const authWindow = window.open("about:blank", "codex-account-login");
    if (authWindow) authWindow.opener = null;
    try {
      const next = await api<CodexLoginSession>("/api/codex/login", { method: "POST" });
      setLogin(next);
      if (authWindow) {
        authWindow.location.replace(next.auth_url);
      } else {
        window.open(next.auth_url, "_blank", "noopener,noreferrer");
      }
    } catch (reason) {
      authWindow?.close();
      setError(reason instanceof Error ? reason.message : "启动 Codex 登录失败");
    } finally {
      setWorking(false);
    }
  }

  async function cancelLogin() {
    if (!login) return;
    setWorking(true);
    setError("");
    try {
      setLogin(await api<CodexLoginSession>(`/api/codex/login/${login.session_id}/cancel`, { method: "POST" }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消 Codex 登录失败");
    } finally {
      setWorking(false);
    }
  }

  const limits = account?.rate_limits?.rateLimitsByLimitId?.length
    ? account.rate_limits.rateLimitsByLimitId
    : account?.rate_limits?.rateLimits
      ? [account.rate_limits.rateLimits]
      : [];
  const accountName = account?.account?.email ?? "已登录的 ChatGPT 账户";

  return (
    <section className="section-card codex-account-card">
      <div className="section-title-row codex-account-head">
        <div>
          <h2>独立 Codex Home 账户</h2>
          <p>登录态只写入已保存的独立目录；页面不会读取或展示认证凭据。</p>
        </div>
        {props.configuredHome && (
          <div className="button-group">
            <button className="button secondary" disabled={loading || working} onClick={() => { void refresh(); }}>刷新</button>
            {login?.status === "pending" ? (
              <>
                <button className="button secondary" onClick={() => window.open(login.auth_url, "_blank", "noopener,noreferrer")}>打开登录页</button>
                <button className="button danger" disabled={working} onClick={() => { void cancelLogin(); }}>取消登录</button>
              </>
            ) : (
              <button className="button primary" disabled={loading || working || props.homeHasUnsavedChange} onClick={() => { void startLogin(); }}>
                {working ? "启动中…" : account?.status === "signed_in" ? "重新登录" : "登录"}
              </button>
            )}
          </div>
        )}
      </div>

      {!props.configuredHome && (
        <div className="codex-account-empty">
          当前未配置独立 Codex Home，后台继续继承环境中的 <code>CODEX_HOME</code> 或 <code>~/.codex</code>；账户登录仍按原方式管理。
        </div>
      )}
      {props.configuredHome && (
        <>
          <div className="agent-workspace-note codex-home-note"><strong>已保存目录</strong><span>{props.configuredHome}</span></div>
          {props.homeHasUnsavedChange && <div className="alert error">Codex Home 有未保存修改。请先保存或取消修改，再管理该目录的登录态。</div>}
          {error && <div className="alert error">{error}</div>}
          {login?.status === "pending" && <div className="alert success">浏览器登录进行中；完成授权后，本页会自动刷新账户信息。</div>}
          {login?.status === "failed" && <div className="alert error">{login.error || "Codex 登录失败"}</div>}
          {login?.status === "cancelled" && <div className="codex-account-empty">本次登录已取消。</div>}
          {loading && <div className="codex-account-empty"><span className="spinner" />正在读取账户与额度…</div>}
          {!loading && account?.status === "signed_out" && <div className="codex-account-empty">该独立 Codex Home 尚未登录。</div>}
          {!loading && account?.status === "signed_in" && (
            <div className="codex-account-content">
              <dl className="codex-account-details">
                <div><dt>账户</dt><dd>{accountName}</dd></div>
                <div><dt>账户类型</dt><dd>{account.account?.type === "chatgpt" ? "ChatGPT" : account.account?.type ?? "—"}</dd></div>
                <div><dt>套餐</dt><dd>{account.account?.planType ?? "未提供"}</dd></div>
                <div><dt>凭据来源</dt><dd>{account.account?.credentialSource ?? "Codex Home"}</dd></div>
              </dl>
              <div className="codex-quota-list">
                <div className="codex-quota-title"><strong>额度</strong><span>{limits.length ? `${limits.length} 个额度窗口` : "当前 CLI 未返回额度信息"}</span></div>
                {limits.map((limit, index) => (
                  <div className="codex-quota-item" key={limit.limitId ?? `${limit.limitName ?? "limit"}-${index}`}>
                    <strong>{limit.limitName ?? limit.limitId ?? `额度 ${index + 1}`}</strong>
                    <CodexQuotaWindow label="主要" value={limit.primary} />
                    <CodexQuotaWindow label="次要" value={limit.secondary} />
                  </div>
                ))}
                {account.rate_limits?.rateLimitResetCredits && (
                  <small className="codex-reset-credits">可用额度重置次数：{account.rate_limits.rateLimitResetCredits.availableCount}</small>
                )}
                {account.rate_limits_error && <small className="codex-account-warning">{account.rate_limits_error}</small>}
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function ConfigHistory() {
  const [versions, setVersions] = useState<Array<{
    revision: string;
    source: string;
    created_at: number;
  }>>([]);
  const [content, setContent] = useState("");

  useEffect(() => {
    void api<typeof versions>("/api/config/versions?limit=20")
      .then(setVersions)
      .catch(() => undefined);
  }, []);

  return (
    <section className="section-card">
      <div className="section-title-row">
        <div><h2>配置历史</h2><p>只记录通过校验且已经脱敏的版本快照。</p></div>
      </div>
      <div className="history-layout">
        <div className="history-list">
          {versions.map((version) => (
            <button
              key={version.revision}
              onClick={() => {
                void api<{ content: string }>(`/api/config/versions/${version.revision}`)
                  .then((result) => setContent(result.content));
              }}
            >
              <code>{shortRevision(version.revision)}</code>
              <span>{version.source === "ui" ? "UI 保存" : version.source === "file" ? "文件热加载" : "服务启动"}</span>
              <small>{timeText(version.created_at)}</small>
            </button>
          ))}
          {versions.length === 0 && <div className="empty">暂无配置历史</div>}
        </div>
        <pre className="detail-pre history-content">{content || "选择左侧版本查看脱敏后的 YAML"}</pre>
      </div>
    </section>
  );
}

function RepositoriesEditor(props: {
  document: ConfigDocument;
  onChange: (document: ConfigDocument) => void;
}) {
  const providerNames = Object.keys(props.document.providers);
  const protectedNames = providerCredentialNames(props.document);
  const repositoryCount = props.document.repositories.length;

  function providerDefaults(kind: "github" | "gitlab") {
    return kind === "gitlab"
      ? { base_url: "https://gitlab.com/api/v4", token_env: "GITLAB_TOKEN" }
      : { base_url: "https://api.github.com", token_env: "GITHUB_TOKEN" };
  }

  function addProvider() {
    let index = providerNames.length + 1;
    let name = `provider-${index}`;
    while (props.document.providers[name]) name = `provider-${++index}`;
    props.onChange({
      ...props.document,
      providers: {
        ...props.document.providers,
        [name]: { kind: "github", ...providerDefaults("github") },
      },
    });
  }

  function updateRepository(index: number, patch: Partial<Repository>) {
    const repositories = [...props.document.repositories];
    repositories[index] = { ...repositories[index], ...patch };
    props.onChange({ ...props.document, repositories });
  }

  function updateProvider(name: string, patch: Record<string, unknown>) {
    props.onChange({
      ...props.document,
      providers: {
        ...props.document.providers,
        [name]: { ...props.document.providers[name], ...patch },
      },
    });
  }

  function addRepository() {
    let index = props.document.repositories.length + 1;
    let id = `repository-${index}`;
    while (props.document.repositories.some((repository) => repository.id === id)) {
      id = `repository-${++index}`;
    }
    props.onChange({
      ...props.document,
      repositories: [...props.document.repositories, {
        id,
        provider: providerNames[0],
        project: "owner/repository",
        workspace: `./workspaces/${id}`,
        enabled: false,
        environment: {},
      }],
    });
  }

  function renameProvider(name: string, nextName: string): boolean {
    if (!nextName || (nextName !== name && props.document.providers[nextName])) return false;
    const providers = Object.fromEntries(
      Object.entries(props.document.providers).map(([key, value]) => [
        key === name ? nextName : key,
        value,
      ]),
    );
    const repositories = props.document.repositories.map((repository) => ({
      ...repository,
      provider: repository.provider === name ? nextName : repository.provider,
    }));
    props.onChange({ ...props.document, providers, repositories });
    return true;
  }

  function renameRepository(index: number, id: string): boolean {
    if (!id || props.document.repositories.some((item, itemIndex) => itemIndex !== index && item.id === id)) return false;
    const oldId = props.document.repositories[index].id;
    const repositories = [...props.document.repositories];
    const oldDefaultWorkspace = `./workspaces/${oldId}`;
    repositories[index] = {
      ...repositories[index],
      id,
      workspace: repositories[index].workspace === oldDefaultWorkspace
        ? `./workspaces/${id}`
        : repositories[index].workspace,
    };
    const rules = props.document.rules.map((rule) => ({
      ...rule,
      repositories: rule.repositories?.map((repositoryId) => repositoryId === oldId ? id : repositoryId),
    }));
    props.onChange({ ...props.document, repositories, rules });
    return true;
  }

  return (
    <div className="page-stack">
      <section className="setup-flow" aria-label="仓库配置步骤">
        <div className={`setup-step ${providerNames.length > 0 ? "complete" : "current"}`}>
          <span>1</span>
          <div><strong>连接 GitHub / GitLab</strong><small>配置平台 API 和 Token 环境变量名</small></div>
        </div>
        <div className={`setup-step ${repositoryCount > 0 ? "complete" : providerNames.length > 0 ? "current" : ""}`}>
          <span>2</span>
          <div><strong>添加仓库</strong><small>关联远端项目与本地工作目录</small></div>
        </div>
        <div className={`setup-step ${props.document.repositories.some((repository) => repository.enabled) ? "complete" : repositoryCount > 0 ? "current" : ""}`}>
          <span>3</span>
          <div><strong>启用扫描</strong><small>保存配置后后台开始定时扫描</small></div>
        </div>
      </section>
      <section className="section-card">
        <div className="section-title-row">
          <div>
            <h2>GitHub / GitLab 连接</h2>
            <p>后台使用这里的平台 API 和 Token 扫描远端 MR / PR；平台连接本身不会立即克隆代码，Agent 首次运行时才按仓库配置自动准备本地工作目录。</p>
          </div>
          <button className="button secondary" onClick={addProvider}>+ 添加平台连接</button>
        </div>
        <div className="card-list compact">
          {providerNames.length === 0 && (
            <div className="empty-config-state">
              <strong>还没有 GitHub / GitLab 连接</strong>
              <p>先点击“添加平台连接”，再配置平台 API 地址，以及宿主机中保存访问 Token 的环境变量名。</p>
            </div>
          )}
          {providerNames.map((name) => {
            const provider = props.document.providers[name];
            const tokenEnvironment = String(provider.token_env ?? "").trim();
            const hasGlobalToken = Boolean(tokenEnvironment)
              && Object.hasOwn(props.document.environment.global, tokenEnvironment);
            const tokenHelp = !tokenEnvironment
              ? "填写 Provider Token 的变量名"
              : hasGlobalToken
                ? "已由“全局环境”配置；只供后台扫描器使用，不会进入 Prompt 或 Codex 进程"
                : `全局环境未配置；将从启动服务的宿主机环境 ${tokenEnvironment} 读取`;
            const referencedRepositories = props.document.repositories.filter((repository) => repository.provider === name).length;
            return (
              <div className="sub-card provider-row" key={name}>
                <CommitField label="连接名称" value={name} onCommit={(nextName) => renameProvider(name, nextName)} />
                <label className="field"><span>代码平台</span><select value={String(provider.kind)} onChange={(event) => {
                  const kind = event.target.value as "github" | "gitlab";
                  updateProvider(name, { kind, ...providerDefaults(kind) });
                }}><option value="github">GitHub</option><option value="gitlab">GitLab</option></select></label>
                <Field label="平台 API 地址" value={String(provider.base_url ?? "")} onChange={(value) => updateProvider(name, { base_url: value })} help="自建 GitHub Enterprise / GitLab 时改为实际 API 地址" />
                <Field label="Provider Token 变量名" value={String(provider.token_env ?? "")} onChange={(value) => updateProvider(name, { token_env: value })} help={tokenHelp} />
                <button
                  className="icon-button danger align-end"
                  disabled={referencedRepositories > 0}
                  title={referencedRepositories > 0 ? `有 ${referencedRepositories} 个仓库正在使用此连接` : "删除连接"}
                  onClick={() => {
                    const providers = { ...props.document.providers };
                    delete providers[name];
                    props.onChange({ ...props.document, providers });
                  }}
                >×</button>
              </div>
            );
          })}
        </div>
      </section>
      <section className="section-card">
        <div className="section-title-row">
          <div><h2>仓库</h2><p>选择上方的平台连接扫描远端 MR / PR；基础 Git 仓库用于 fetch，并为每次 Agent 运行创建独立临时 worktree。</p></div>
          <button
            className="button primary"
            disabled={providerNames.length === 0}
            title={providerNames.length === 0 ? "请先添加 GitHub / GitLab 连接" : "添加仓库"}
            onClick={addRepository}
          >+ 添加仓库</button>
        </div>
        <div className="card-list">
          {props.document.repositories.length === 0 && (
            <div className="empty-config-state">
              <strong>{providerNames.length === 0 ? "请先连接 GitHub 或 GitLab" : "还没有配置仓库"}</strong>
              <p>{providerNames.length === 0 ? "添加平台连接后，才可以创建仓库配置。" : "点击“添加仓库”，关联远端项目和已经准备好的本地 Git 工作目录。"}</p>
            </div>
          )}
          {props.document.repositories.map((repository, index) => (
            <article className="sub-card" key={index}>
              <div className="sub-card-head">
                <div><h3>{repository.id || "未命名仓库"}</h3><p>{repository.project}</p></div>
                <div className="button-group"><Toggle label="启用" checked={repository.enabled ?? true} onChange={(enabled) => updateRepository(index, { enabled })} /><button className="icon-button danger" onClick={() => props.onChange({ ...props.document, repositories: props.document.repositories.filter((_, itemIndex) => itemIndex !== index) })}>×</button></div>
              </div>
              <div className="form-grid two">
                <CommitField label="仓库 ID" value={repository.id} onCommit={(id) => renameRepository(index, id)} />
                <label className="field"><span>所属 GitHub / GitLab 连接</span><select value={repository.provider} onChange={(event) => updateRepository(index, { provider: event.target.value })}>{providerNames.map((provider) => <option key={provider}>{provider}</option>)}</select><small>决定使用哪个平台 API 和 Token 扫描此仓库</small></label>
                <Field label="远端仓库地址 / 项目路径" value={repository.clone_url ?? repository.project} onChange={(project) => updateRepository(index, { project, clone_url: undefined })} placeholder="git@github.com:owner/repository.git" help="支持 owner/repository、group/project、SSH 或 HTTPS Git 地址；保存后后台会自动解析为平台项目路径" />
                <Field label="基础 Git 仓库目录（自动管理）" value={repository.workspace} onChange={(workspace) => updateRepository(index, { workspace })} help="默认使用 ./workspaces/<仓库ID>；这里只负责克隆、校验、fetch 和管理 worktree，Codex 实际在每次运行独享的临时目录中工作" />
              </div>
              <EnvironmentEditor compact title="仓库环境变量" value={repository.environment ?? {}} protectedNames={protectedNames} onChange={(environment) => updateRepository(index, { environment })} />
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}

function SkillsEditor(props: {
  document: ConfigDocument;
  onChange: (document: ConfigDocument) => void;
}) {
  const [directories, setDirectories] = useState<ManagedSkillDirectory[]>([]);
  const [inspected, setInspected] = useState<Record<string, ManagedSkillDirectory>>({});
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const skillEntries = Object.entries(props.document.skills);
  const skillSignature = JSON.stringify(skillEntries.map(([id, skill]) => [id, skill.path]));

  const loadDirectories = useCallback(async () => {
    try {
      setDirectories(await api<ManagedSkillDirectory[]>("/api/skill-directories"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取 Skill 目录失败");
    }
  }, []);

  useEffect(() => { void loadDirectories(); }, [loadDirectories]);
  useEffect(() => {
    let cancelled = false;
    async function inspectConfiguredSkills() {
      const results = await Promise.all(skillEntries.map(async ([id, skill]) => {
        try {
          const metadata = await api<ManagedSkillDirectory>("/api/skill-directories/inspect", {
            method: "POST",
            body: JSON.stringify({ path: skill.path }),
          });
          return [id, metadata] as const;
        } catch (reason) {
          return [id, {
            path: skill.path,
            name: id,
            description: "",
            valid: false,
            error: reason instanceof Error ? reason.message : "Skill 路径检查失败",
          }] as const;
        }
      }));
      if (!cancelled) setInspected(Object.fromEntries(results));
    }
    void inspectConfiguredSkills();
    return () => { cancelled = true; };
  }, [skillSignature]);

  function uniqueId(name: string): string {
    const normalized = name
      .toLowerCase()
      .replace(/[^a-z0-9._-]+/g, "-")
      .replace(/^[-._]+|[-._]+$/g, "") || "skill";
    let id = normalized;
    let index = 2;
    while (props.document.skills[id]) id = `${normalized}-${index++}`;
    return id;
  }

  function addSkill(skill?: ManagedSkillDirectory) {
    const id = uniqueId(skill?.name ?? `skill-${skillEntries.length + 1}`);
    const value: Skill = { path: skill?.path ?? `./skills/${id}` };
    props.onChange({
      ...props.document,
      skills: { ...props.document.skills, [id]: value },
    });
  }

  function renameSkill(id: string, nextId: string): boolean {
    if (!nextId || (nextId !== id && props.document.skills[nextId])) return false;
    const skills = Object.fromEntries(
      Object.entries(props.document.skills).map(([key, value]) => [
        key === id ? nextId : key,
        value,
      ]),
    );
    const agents = Object.fromEntries(
      Object.entries(props.document.agents).map(([name, agent]) => [
        name,
        {
          ...agent,
          skills: agent.skills?.map((skillId) => skillId === id ? nextId : skillId),
        },
      ]),
    );
    props.onChange({ ...props.document, skills, agents });
    return true;
  }

  async function importDirectory(files: File[]) {
    if (files.length === 0) return;
    setUploading(true);
    setMessage("");
    setError("");
    try {
      const imported = await uploadSkillDirectory(files);
      addSkill(imported);
      setMessage(`已导入并加入配置：${imported.name}`);
      await loadDirectories();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "导入 Skill 文件夹失败");
    } finally {
      setUploading(false);
    }
  }

  const configuredPaths = new Set(skillEntries.map(([, skill]) => skill.path));
  const availableDirectories = directories.filter((item) => !configuredPaths.has(item.path));

  return (
    <div className="page-stack">
      <section className="section-card">
        <div className="section-title-row">
          <div>
            <h2>Codex Skill</h2>
            <p>注册包含 SKILL.md 的技能目录；每个 Agent 再独立选择允许装载的 Skill。</p>
          </div>
          <div className="button-group">
            <button className="button secondary" type="button" onClick={() => addSkill()}>+ 配置服务端目录</button>
            <label className={`button primary file-button ${uploading ? "disabled" : ""}`}>
              {uploading ? "正在导入…" : "从电脑导入文件夹"}
              <input
                type="file"
                multiple
                disabled={uploading}
                ref={(input) => {
                  if (input) {
                    input.setAttribute("webkitdirectory", "");
                    input.setAttribute("directory", "");
                  }
                }}
                onChange={(event) => {
                  void importDirectory(Array.from(event.target.files ?? []));
                  event.target.value = "";
                }}
              />
            </label>
          </div>
        </div>
        <div className="agent-workspace-note">
          <strong>原生 Skill 装载</strong>
          <span>目录根部必须有带 name 和 description 的 SKILL.md；scripts、references、assets 等子目录会完整保留。选择 Skill 只是让 Codex 可以按描述匹配或通过 $skill-name 显式使用，不代表每次都强制执行。</span>
        </div>
        {message && <p className="inline-message success-text skill-message">{message}</p>}
        {error && <p className="inline-message error-text skill-message">{error}</p>}
        <div className="card-list compact">
          {skillEntries.map(([id, skill]) => {
            const metadata = inspected[id];
            return (
              <article className="sub-card skill-card" key={id}>
                <div className="sub-card-head">
                  <div className="skill-heading">
                    <CommitField label="配置 ID" value={id} onCommit={(nextId) => renameSkill(id, nextId)} />
                    {metadata && (
                      <div className={`skill-metadata ${metadata.valid ? "valid" : "invalid"}`}>
                        <strong>{metadata.valid ? metadata.name : "校验失败"}</strong>
                        <span>{metadata.valid ? metadata.description : metadata.error}</span>
                      </div>
                    )}
                  </div>
                  <button className="icon-button danger" type="button" onClick={() => {
                    const skills = { ...props.document.skills };
                    delete skills[id];
                    const agents = Object.fromEntries(
                      Object.entries(props.document.agents).map(([name, agent]) => [
                        name,
                        { ...agent, skills: agent.skills?.filter((skillId) => skillId !== id) },
                      ]),
                    );
                    props.onChange({ ...props.document, skills, agents });
                  }}>×</button>
                </div>
                <Field
                  label="Skill 文件夹路径"
                  value={skill.path}
                  onChange={(path) => props.onChange({
                    ...props.document,
                    skills: { ...props.document.skills, [id]: { path } },
                  })}
                  placeholder="./skills/my-skill"
                  help="相对路径以 config.yaml 所在目录为基准，也可以填写服务端绝对路径"
                />
              </article>
            );
          })}
          {skillEntries.length === 0 && (
            <div className="empty-config-state">
              <strong>还没有配置 Skill</strong>
              <p>可以从电脑选择整个 Skill 文件夹，或填写服务端已经存在的目录。导入文件会保存到 `./skills/`，但只有加入配置并被 Agent 选中后才会装载。</p>
            </div>
          )}
        </div>
      </section>
      {availableDirectories.length > 0 && (
        <section className="section-card">
          <div className="section-title-row"><div><h2>已导入但未配置</h2><p>这些目录保留在 `./skills/`，可以重新加入当前配置。</p></div></div>
          <div className="managed-skill-list">
            {availableDirectories.map((skill) => (
              <div className={`managed-skill ${skill.valid ? "" : "invalid"}`} key={skill.path}>
                <div><strong>{skill.name}</strong><span>{skill.valid ? skill.description : skill.error}</span><code>{skill.path}</code></div>
                <button className="button secondary compact" type="button" disabled={!skill.valid} onClick={() => addSkill(skill)}>加入配置</button>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function AgentsEditor(props: {
  document: ConfigDocument;
  codexOptions: CodexRuntimeOptions;
  onChange: (document: ConfigDocument) => void;
}) {
  const names = Object.keys(props.document.agents);
  const skillOptions = Object.entries(props.document.skills).map(([skillId, skill]) => ({
    value: skillId,
    label: skillId,
    description: skill.path,
  }));
  const protectedNames = providerCredentialNames(props.document);
  const inheritedModel = effectiveInheritedModel(props.document, props.codexOptions);
  function update(name: string, patch: Partial<Agent>) {
    props.onChange({ ...props.document, agents: { ...props.document.agents, [name]: { ...props.document.agents[name], ...patch } } });
  }
  function rename(name: string, nextName: string): boolean {
    if (!nextName || (nextName !== name && props.document.agents[nextName])) return false;
    const agents = Object.fromEntries(
      Object.entries(props.document.agents).map(([key, value]) => [
        key === name ? nextName : key,
        {
          ...value,
          allowed_sub_agents: value.allowed_sub_agents?.map((item) => item === name ? nextName : item),
        },
      ]),
    );
    const rules = props.document.rules.map((rule) => ({
      ...rule,
      agents: rule.agents.map((item) => item === name ? nextName : item),
    }));
    props.onChange({ ...props.document, agents, rules });
    return true;
  }
  return (
    <section className="section-card">
      <div className="section-title-row">
        <div><h2>Agent</h2><p>每个 Agent 由 Codex CLI 执行，并可通过白名单调用其他 Agent。</p></div>
        <button className="button primary" onClick={() => {
          let index = names.length + 1;
          let name = `agent-${index}`;
          while (props.document.agents[name]) name = `agent-${++index}`;
          props.onChange({ ...props.document, agents: { ...props.document.agents, [name]: { prompt: "请处理当前 MR / PR。", sandbox: "read-only", network_access: false, network_domains: [], timeout_seconds: 1200, write_scopes: [], allowed_sub_agents: [], skills: [], environment: {} } } });
        }}>+ 添加 Agent</button>
      </div>
      <div className="agent-workspace-note">
        <strong>每次运行使用独立 worktree</strong>
        <span>Agent 本身不绑定固定目录；仓库配置目录只作基础仓库。每次根 Agent 在 MR / PR Head 上创建独立临时 worktree，不同分支可以并发；声明“本地仓库写操作”后，同一源分支会串行。sub-agent 是否复用父 Agent 当前 worktree，由触发规则中的“继承当前工作区”决定。</span>
      </div>
      <div className="card-list">
        {names.map((name) => {
          const agent = props.document.agents[name];
          const promptSource = agent.prompt_file ? "file" : "inline";
          const writeScopes = agent.write_scopes ?? [];
          const subAgentOptions = names
            .filter((item) => item !== name)
            .map((item) => {
              const candidate = props.document.agents[item];
              return {
                value: item,
                label: item,
                description: `${candidate.sandbox ?? "read-only"} · ${candidate.prompt_file ? "文件 Prompt" : "内联 Prompt"}`,
              };
            });
          return (
            <article className="sub-card" key={name}>
              <div className="sub-card-head">
                <div className="agent-name"><span className="eyebrow">CODEX AGENT</span><CommitField label="Agent 名称" value={name} onCommit={(nextName) => rename(name, nextName)} /></div>
                <button className="icon-button danger" onClick={() => {
                  const agents = { ...props.document.agents };
                  delete agents[name];
                  const cleanedAgents = Object.fromEntries(
                    Object.entries(agents).map(([agentName, value]) => [
                      agentName,
                      {
                        ...value,
                        allowed_sub_agents: value.allowed_sub_agents?.filter((item) => item !== name),
                      },
                    ]),
                  );
                  const rules = props.document.rules.map((rule) => ({
                    ...rule,
                    agents: rule.agents.filter((item) => item !== name),
                  }));
                  props.onChange({ ...props.document, agents: cleanedAgents, rules });
                }}>×</button>
              </div>
              <div className="form-grid three">
                <ModelField
                  id={`agent-models-${name.replace(/[^A-Za-z0-9_-]/g, "-")}`}
                  label="模型（可选）"
                  value={agent.model ?? ""}
                  placeholder={inheritedModel.label}
                  models={props.codexOptions.models}
                  onChange={(model) => update(name, { model: model || undefined })}
                  help={agent.model ? "当前 Agent 显式覆盖运行时默认" : inheritedModel.label}
                />
                <label className="field"><span>本地文件权限（Sandbox）</span><select value={agent.sandbox ?? "read-only"} onChange={(event) => {
                  const sandbox = event.target.value as Agent["sandbox"];
                  const nextScopes = sandbox === "read-only"
                    ? writeScopes.filter((scope) => scope !== "workspace")
                    : Array.from(new Set([...writeScopes, "workspace"]));
                  update(name, {
                    sandbox,
                    write_scopes: nextScopes as Agent["write_scopes"],
                    network_access: sandbox === "danger-full-access"
                      ? true
                      : sandbox === "read-only" || agent.sandbox === "danger-full-access"
                        ? false
                        : agent.network_access ?? false,
                    network_domains: sandbox === "workspace-write" ? agent.network_domains ?? [] : [],
                  });
                }}><option value="read-only">只读：不能修改本地文件</option><option value="workspace-write">工作区可写：可修改仓库文件</option><option value="danger-full-access">完全访问：高风险</option></select><small>切换为可写模式时会自动启用“本地仓库写操作”</small></label>
                <Field label="总超时（秒）" type="number" value={agent.timeout_seconds ?? 1200} onChange={(value) => update(name, { timeout_seconds: Number(value) })} />
                <Field label="无进展超时（秒，可选）" type="number" value={agent.idle_timeout_seconds ?? ""} placeholder={`继承运行时默认 ${String(props.document.runtime.agent_idle_timeout_seconds ?? 300)}`} onChange={(value) => update(name, { idle_timeout_seconds: value ? Number(value) : undefined })} />
                <Field label="输出 Schema（可选）" value={agent.output_schema ?? ""} onChange={(output_schema) => update(name, { output_schema: output_schema || undefined })} />
              </div>
              <div className="form-grid three agent-runtime-overrides">
                <SelectField
                  label="推理强度（可选）"
                  value={agent.model_reasoning_effort ?? ""}
                  onChange={(value) => update(name, { model_reasoning_effort: value || undefined })}
                  options={[
                    { value: "", label: "继承运行时默认" },
                    ...reasoningLevels(
                      props.codexOptions,
                      agent.model || inheritedModel.value,
                      agent.model_reasoning_effort,
                    ).map((value) => ({ value, label: value })),
                  ]}
                />
                <SelectField
                  label="快速模式（可选）"
                  value={agent.fast_mode ?? "inherit"}
                  onChange={(value) => update(name, { fast_mode: value as Agent["fast_mode"] })}
                  options={[
                    { value: "inherit", label: "继承运行时默认" },
                    { value: "standard", label: "标准模式" },
                    { value: "fast", label: "快速模式" },
                  ]}
                />
                <SelectField
                  label="输出详细度（可选）"
                  value={agent.model_verbosity ?? ""}
                  onChange={(value) => update(name, { model_verbosity: value ? value as Agent["model_verbosity"] : undefined })}
                  options={[
                    { value: "", label: "继承运行时默认" },
                    { value: "low", label: "低" },
                    { value: "medium", label: "中" },
                    { value: "high", label: "高" },
                  ]}
                />
                <SelectField
                  label="交互风格（可选）"
                  value={agent.personality ?? ""}
                  onChange={(value) => update(name, { personality: value ? value as Agent["personality"] : undefined })}
                  options={[
                    { value: "", label: "继承运行时默认" },
                    { value: "none", label: "无预设" },
                    { value: "friendly", label: "友好" },
                    { value: "pragmatic", label: "务实" },
                  ]}
                />
                <SelectField
                  label="联网搜索（可选）"
                  value={agent.web_search ?? ""}
                  onChange={(value) => update(name, { web_search: value ? value as Agent["web_search"] : undefined })}
                  options={[
                    { value: "", label: "继承运行时默认" },
                    { value: "disabled", label: "禁用" },
                    { value: "cached", label: "缓存搜索" },
                    { value: "live", label: "实时搜索" },
                  ]}
                />
              </div>
              <section className="network-permission-section">
                <div className="network-permission-head">
                  <div>
                    <strong>命令联网权限</strong>
                    <p>控制 shell、gh、glab 等命令访问网络，与上面的联网搜索设置相互独立。</p>
                  </div>
                  <Toggle
                    label={agent.sandbox === "danger-full-access" ? "网络不受沙箱限制" : "允许命令联网"}
                    checked={agent.sandbox === "danger-full-access" || (agent.network_access ?? false)}
                    disabled={agent.sandbox !== "workspace-write"}
                    onChange={(network_access) => update(name, {
                      network_access,
                      network_domains: network_access ? agent.network_domains ?? [] : [],
                    })}
                  />
                </div>
                {agent.sandbox === "workspace-write" && agent.network_access ? (
                  <NetworkDomainsField
                    value={agent.network_domains ?? []}
                    onChange={(network_domains) => update(name, { network_domains })}
                  />
                ) : (
                  <p className="network-permission-state">
                    {agent.sandbox === "read-only"
                      ? "只读沙箱不支持通过本配置开放命令联网。"
                      : agent.sandbox === "danger-full-access"
                        ? "完全访问模式本身允许联网，域名白名单无法形成可靠隔离。"
                        : "命令联网已关闭。"}
                  </p>
                )}
                <p className="network-credential-note">
                  Provider Token 始终不会进入 Codex；gh / glab 只使用当前系统钥匙串或各自 CLI 登录态。
                </p>
              </section>
              <div className="permissions-grid">
                <ChoiceCards
                  title="写操作声明"
                  description="用于申请串行资源锁和记录权限边界，不等同于平台账号授权。"
                  values={writeScopes}
                  options={[
                    { value: "change_request", label: "MR / PR 写操作", description: "评论、标签、审批或合并时锁定当前变更请求" },
                    { value: "workspace", label: "本地仓库写操作", description: "修改、提交或推送时锁定当前仓库的 MR / PR 源分支" },
                  ]}
                  onChange={(values) => {
                    const hasWorkspace = values.includes("workspace");
                    update(name, {
                      write_scopes: values as Agent["write_scopes"],
                      sandbox: hasWorkspace
                        ? agent.sandbox === "danger-full-access" ? "danger-full-access" : "workspace-write"
                        : "read-only",
                      network_access: hasWorkspace ? agent.network_access ?? false : false,
                      network_domains: hasWorkspace ? agent.network_domains ?? [] : [],
                    });
                  }}
                />
                <ChoiceCards
                  title="允许调用的 sub-agent"
                  description="只是授予 invoke_agent 委托权限，不会自动运行；sub-agent 使用自己的 Skill 配置。"
                  values={agent.allowed_sub_agents ?? []}
                  options={subAgentOptions}
                  emptyText="暂无其他 Agent。请先创建另一个 Agent，再回来授予调用权限。"
                  onChange={(allowed_sub_agents) => update(name, { allowed_sub_agents })}
                />
              </div>
              <div className="agent-skill-section">
                <ChoiceCards
                  title="本 Agent 装载的 Skill"
                  description="只影响当前 Agent；sub-agent 不继承这里的选择，即使复用父 Agent 工作区也使用它自己的 Skill 列表。"
                  values={agent.skills ?? []}
                  options={skillOptions}
                  emptyText="暂无已配置 Skill。请先到左侧“SKILL”页面导入或配置。"
                  onChange={(skills) => update(name, { skills })}
                />
              </div>
              <div className="prompt-editor">
                <div className="prompt-toolbar">
                  <label>Prompt 来源 <select value={promptSource} onChange={(event) => update(name, event.target.value === "file" ? { prompt_file: "./prompts/agent.md", prompt: undefined } : { prompt: "请处理当前 MR / PR。", prompt_file: undefined })}><option value="file">文件</option><option value="inline">内联模板</option></select></label>
                  <code>{"支持 ${{ENV_NAME}}，缺失变量会渲染为空"}</code>
                </div>
                {promptSource === "file" ? (
                  <PromptFilePicker value={agent.prompt_file ?? ""} onChange={(prompt_file) => update(name, { prompt_file })} />
                ) : (
                  <textarea value={agent.prompt ?? ""} onChange={(event) => update(name, { prompt: event.target.value })} rows={8} />
                )}
              </div>
              <div className="toggle-grid">
                <Toggle label="跳过 Git 仓库检查" checked={agent.skip_git_repo_check ?? false} onChange={(skip_git_repo_check) => update(name, { skip_git_repo_check })} />
              </div>
              <EnvironmentEditor compact title="Agent 环境变量" value={agent.environment ?? {}} protectedNames={protectedNames} onChange={(environment) => update(name, { environment })} />
            </article>
          );
        })}
      </div>
    </section>
  );
}

function JsonEditor(props: {
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
  label?: string;
  help?: string;
}) {
  const [text, setText] = useState(JSON.stringify(props.value, null, 2));
  const [error, setError] = useState("");
  useEffect(() => setText(JSON.stringify(props.value, null, 2)), [props.value]);
  function apply() {
    try {
      const parsed = JSON.parse(text) as Record<string, unknown>;
      props.onChange(parsed);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "JSON 格式错误");
    }
  }
  return (
    <label className="field json-field">
      <span>{props.label ?? "条件（JSON）"}</span>
      <textarea className="mono" rows={7} value={text} onChange={(event) => setText(event.target.value)} onBlur={apply} />
      {error ? <small className="error-text">{error}</small> : <small>{props.help ?? "字段名可使用 __contains、__gte、__in 等操作符后缀"}</small>}
    </label>
  );
}

function RulesEditor(props: {
  document: ConfigDocument;
  events: string[];
  onChange: (document: ConfigDocument) => void;
}) {
  const agentNames = Object.keys(props.document.agents);
  const repositoryNames = props.document.repositories.map((repository) => repository.id);
  function update(index: number, patch: Partial<Rule>) {
    const rules = [...props.document.rules];
    rules[index] = { ...rules[index], ...patch };
    props.onChange({ ...props.document, rules });
  }
  return (
    <section className="section-card">
      <div className="section-title-row">
        <div><h2>MR / PR 触发规则</h2><p>状态变化生成事件，匹配规则后按顺序触发所选 Agent。</p></div>
        <button className="button primary" onClick={() => props.onChange({ ...props.document, rules: [...props.document.rules, { name: `rule-${props.document.rules.length + 1}`, events: [props.events[0] ?? "change_request.updated"], agents: agentNames.slice(0, 1), conditions: {}, deduplicate_per_scan: false, inherit_workspace: false, enabled: true }] })}>+ 添加规则</button>
      </div>
      <div className="card-list">
        {props.document.rules.map((rule, index) => (
          <article className="sub-card rule-card" key={index}>
            <div className="sub-card-head">
              <div><h3>{rule.name}</h3><p>{rule.events.length} 个事件 · {rule.agents.length} 个 Agent</p></div>
              <div className="button-group"><Toggle label="启用" checked={rule.enabled ?? true} onChange={(enabled) => update(index, { enabled })} /><button className="icon-button danger" onClick={() => props.onChange({ ...props.document, rules: props.document.rules.filter((_, itemIndex) => itemIndex !== index) })}>×</button></div>
            </div>
            <div className="form-grid two">
              <Field label="规则名称" value={rule.name} onChange={(name) => update(index, { name })} />
              <MultiSelect label="触发事件" values={rule.events} options={props.events} onChange={(events) => update(index, { events })} />
              <MultiSelect label="触发 Agent" values={rule.agents} options={agentNames} onChange={(agents) => update(index, { agents })} />
              <MultiSelect label="限制仓库（留空为全部）" values={rule.repositories ?? []} options={repositoryNames} onChange={(repositories) => update(index, { repositories: repositories.length ? repositories : undefined })} />
            </div>
            <div className="rule-options">
              <div className="rule-option">
                <Toggle
                  label="单轮扫描同一 MR / PR 只触发一次"
                  checked={rule.deduplicate_per_scan ?? false}
                  onChange={(deduplicate_per_scan) => update(index, { deduplicate_per_scan })}
                />
                <p>开启后，本轮同一 MR / PR 的多个已选事件会合并为一次 Agent 运行，动作统一写入 mr.action 数组。</p>
              </div>
              <div className="rule-option">
                <Toggle
                  label="sub-agent 继承当前工作区"
                  checked={rule.inherit_workspace ?? false}
                  onChange={(inherit_workspace) => update(index, { inherit_workspace })}
                />
                <p>开启后，sub-agent 复用父 Agent 本次运行的临时 worktree，共享当前分支、暂存区和未提交文件；只继承工作区，不自动继承 MR 输入或父 Agent 对话。</p>
              </div>
            </div>
            <JsonEditor value={rule.conditions ?? {}} onChange={(conditions) => update(index, { conditions })} />
          </article>
        ))}
      </div>
    </section>
  );
}

function StatusPill({ value }: { value: string }) {
  return <span className={`status-pill status-${value}`}>{statusLabel(value)}</span>;
}

function EventStatusPill({ event }: { event: EventRecord }) {
  const labels: Record<string, string> = {
    pending: "待处理",
    processing: "规则匹配中",
    unmatched: "未触发",
    triggered: "已触发",
    completed: event.trigger_count > 0 ? "已处理" : "历史已处理",
    failed: "处理失败",
  };
  return (
    <span className={`status-pill status-${event.status}`}>
      {labels[event.status] ?? event.status}
    </span>
  );
}

function EventAgentProgress({ event }: { event: EventRecord }) {
  const total = event.trigger_count;
  if (total === 0) {
    return <span className="event-agent-none">—</span>;
  }
  const settled = event.agent_completed_count
    + event.agent_failed_count
    + event.agent_timed_out_count
    + event.agent_cancelled_count;
  if (event.agent_running_count > 0) {
    return (
      <span className="event-agent-progress running">
        执行中 {event.agent_running_count} · 已结束 {settled}/{total}
      </span>
    );
  }
  if (event.agent_queued_count > 0) {
    return (
      <span className="event-agent-progress queued">
        排队中 {event.agent_queued_count} · 已结束 {settled}/{total}
      </span>
    );
  }
  const failed = event.agent_failed_count
    + event.agent_timed_out_count
    + event.agent_cancelled_count;
  if (failed > 0) {
    return (
      <span className="event-agent-progress failed">
        异常 {failed} · 已结束 {settled}/{total}
      </span>
    );
  }
  return (
    <span className="event-agent-progress completed">
      已完成 {event.agent_completed_count}/{total}
    </span>
  );
}

type RunDrawerTab = "messages" | "result" | "context";

function durationText(startedAt: number, finishedAt?: number | null): string {
  const totalSeconds = Math.max(0, Math.floor((finishedAt ?? Date.now() / 1000) - startedAt));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}小时 ${minutes}分钟`;
  if (minutes > 0) return `${minutes}分钟 ${seconds}秒`;
  return `${seconds}秒`;
}

function runTargetText(run: RunSummary): string {
  if (run.repository_id && run.change_request_number !== undefined && run.change_request_number !== null) {
    return `${run.repository_id} · #${run.change_request_number}`;
  }
  return run.resource_key;
}

function RunsView(props: { runs: RunSummary[]; onRefresh: () => void }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [logs, setLogs] = useState<RunLog[]>([]);
  const [error, setError] = useState("");
  const [cancelling, setCancelling] = useState(false);
  const [drawerTab, setDrawerTab] = useState<RunDrawerTab>("messages");

  function openRun(runId: string) {
    setSelectedId(runId);
    setDrawerTab("messages");
  }

  function closeDrawer() {
    setSelectedId(null);
    setDetail(null);
    setLogs([]);
    setError("");
  }

  async function cancelSelectedRun() {
    if (!selectedId || !detail || !window.confirm(`确定取消 ${detail.agent_name} 的本次运行吗？\n\n它的全部 sub-agent 也会收到取消请求。`)) return;
    setCancelling(true);
    setError("");
    try {
      const result = await api<{ accepted: boolean; reason: string }>(
        `/api/runs/${encodeURIComponent(selectedId)}/cancel`,
        { method: "POST" },
      );
      if (!result.accepted) setError(result.reason);
      setDetail(await api<RunDetail>(`/api/runs/${encodeURIComponent(selectedId)}`));
      props.onRefresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消运行失败");
    } finally {
      setCancelling(false);
    }
  }

  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    setDetail(null);
    setLogs([]);
    setError("");
    void (async () => {
      try {
        const [run, initialLogs] = await Promise.all([
          api<RunDetail>(`/api/runs/${selectedId}`),
          api<RunLog[]>(`/api/runs/${selectedId}/logs?limit=2000`),
        ]);
        if (controller.signal.aborted) return;
        setDetail(run);
        setLogs(initialLogs);
        const cursor = initialLogs.at(-1)?.id ?? 0;
        await streamRunLogs(selectedId, cursor, controller.signal, (log) => {
          setLogs((current) => current.some((item) => item.id === log.id) ? current : [...current, log]);
        });
        if (!controller.signal.aborted) {
          setDetail(await api<RunDetail>(`/api/runs/${selectedId}`));
          props.onRefresh();
        }
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "日志连接失败");
      }
    })();
    return () => controller.abort();
  }, [selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeDrawer();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [selectedId]);

  return (
    <>
      <section className="section-card runs-list">
        <div className="section-title-row">
          <div><h2>Agent 运行记录</h2><p>按时间查看所有运行；点击一行后从右侧打开消息、结果与上下文。</p></div>
          <button className="button secondary" onClick={props.onRefresh}>刷新</button>
        </div>
        <div className="run-table">
          <div className="run-table-head" aria-hidden="true">
            <span>Agent</span><span>仓库 / MR / PR</span><span>触发来源</span><span>状态</span><span>开始时间</span><span>耗时</span><span />
          </div>
          <div className="run-items">
            {props.runs.map((run) => (
              <button key={run.run_id} className={`run-row ${selectedId === run.run_id ? "selected" : ""}`} onClick={() => openRun(run.run_id)}>
                <span className="run-agent-cell">
                  <span className="run-status-dot" data-status={run.status} />
                  <span><strong>{run.agent_name}</strong><small>{run.run_id.slice(0, 8)}</small></span>
                </span>
                <span className="run-target-cell">
                  <strong>{runTargetText(run)}</strong>
                  <small>{run.change_request_title ?? run.resource_key}</small>
                </span>
                <span className="run-source-cell"><strong>{run.rule_name ?? "Sub-agent 调用"}</strong><small>{run.parent_run_id ? "Sub-agent" : "根 Agent"}</small></span>
                <span><StatusPill value={run.status} />{run.workspace_status === "retained" && <em className="workspace-retained">工作区待清理</em>}</span>
                <span className="run-time-cell"><strong>{timeText(run.started_at)}</strong></span>
                <span className="run-duration-cell"><strong>{durationText(run.started_at, run.finished_at)}</strong></span>
                <span className="run-row-arrow" aria-hidden="true">›</span>
              </button>
            ))}
            {props.runs.length === 0 && <div className="empty">尚无 Agent 运行记录</div>}
          </div>
        </div>
      </section>
      {selectedId && (
        <div className="run-drawer-layer">
          <button className="run-drawer-backdrop" aria-label="关闭运行详情" onClick={closeDrawer} />
          <aside className="run-drawer" role="dialog" aria-modal="true" aria-label="Agent 运行详情">
            <header className="run-drawer-head">
              <div>
                <span className="eyebrow">{detail?.run_id ?? selectedId}</span>
                <h2>{detail?.agent_name ?? "正在加载…"}</h2>
                {detail && <p>{runTargetText(detail)} · {detail.rule_name ?? "Sub-agent 调用"}</p>}
              </div>
              <div className="run-drawer-actions">
                {detail && (detail.status === "queued" || detail.status === "running") && (
                  <button className="button danger" disabled={cancelling} onClick={() => { void cancelSelectedRun(); }}>
                    {cancelling ? "取消中…" : "取消运行"}
                  </button>
                )}
                {detail && <StatusPill value={detail.status} />}
                <button className="run-drawer-close" aria-label="关闭" onClick={closeDrawer}>×</button>
              </div>
            </header>
            <nav className="run-drawer-tabs" aria-label="运行详情分类">
              <button className={drawerTab === "messages" ? "active" : ""} onClick={() => setDrawerTab("messages")}>消息 <span>{logs.length}</span></button>
              <button className={drawerTab === "result" ? "active" : ""} onClick={() => setDrawerTab("result")}>最终结果</button>
              <button className={drawerTab === "context" ? "active" : ""} onClick={() => setDrawerTab("context")}>运行详情</button>
            </nav>
            <div className="run-drawer-body">
              {error && <div className="alert error">{error}</div>}
              {!detail && !error && <div className="empty tall">正在加载运行详情…</div>}
              {detail && drawerTab === "messages" && <RunMessageFeed logs={logs} />}
              {detail && drawerTab === "result" && (
                <div className="run-result-panel">
                  <div className="run-result-summary">
                    <div><span>状态</span><StatusPill value={detail.status} /></div>
                    <div><span>耗时</span><strong>{durationText(detail.started_at, detail.finished_at)}</strong></div>
                    <div><span>配置版本</span><strong>{shortRevision(detail.config_revision)}</strong></div>
                  </div>
                  <h3>Agent 最终消息</h3>
                  {detail.final_message
                    ? <div className="run-result-message"><MarkdownMessage>{detail.final_message}</MarkdownMessage></div>
                    : <pre className={`detail-pre ${detail.error ? "detail-error" : ""}`}>{detail.error ?? "暂无最终消息"}</pre>}
                  {detail.usage && <><h3>用量</h3><pre className="detail-pre">{JSON.stringify(detail.usage, null, 2)}</pre></>}
                </div>
              )}
              {detail && drawerTab === "context" && (
                <div className="detail-tabs run-context-panel">
                  <details open>
                    <summary>运行信息</summary>
                    <dl className="run-metadata">
                      <div><dt>运行 ID</dt><dd>{detail.run_id}</dd></div>
                      <div><dt>触发规则</dt><dd>{detail.rule_name ?? "Sub-agent 调用"}</dd></div>
                      <div><dt>开始时间</dt><dd>{timeText(detail.started_at)}</dd></div>
                      <div><dt>结束时间</dt><dd>{timeText(detail.finished_at)}</dd></div>
                      <div><dt>Codex 会话</dt><dd>{detail.thread_id ?? "—"}</dd></div>
                    </dl>
                  </details>
                  <details><summary>渲染后的 Prompt</summary><pre className="detail-pre">{detail.prompt}</pre></details>
                  <details><summary>环境变量审计</summary><pre className="detail-pre">{JSON.stringify(detail.environment, null, 2)}</pre></details>
                  <details>
                    <summary>运行工作区 <span className={`workspace-state workspace-${detail.workspace_status ?? "unknown"}`}>{workspaceStatusLabel(detail.workspace_status)}</span></summary>
                    <pre className="detail-pre">{[
                      detail.workspace_path ? `路径：${detail.workspace_path}` : "路径：未创建",
                      detail.workspace_reason ? `说明：${detail.workspace_reason}` : "",
                    ].filter(Boolean).join("\n")}</pre>
                  </details>
                  {detail.children.length > 0 && (
                    <details open>
                      <summary>Sub-agent <span>{detail.children.length}</span></summary>
                      <div className="children-list">{detail.children.map((child) => <button key={child.run_id} onClick={() => openRun(child.run_id)}>{child.agent_name}<StatusPill value={child.status} /></button>)}</div>
                    </details>
                  )}
                </div>
              )}
            </div>
          </aside>
        </div>
      )}
    </>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("overview");
  const [document, setDocument] = useState<ConfigDocument | null>(null);
  const [savedDocument, setSavedDocument] = useState<ConfigDocument | null>(null);
  const [status, setStatus] = useState<RuntimeStatus>(EMPTY_STATUS);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [changeRequests, setChangeRequests] = useState<ChangeRequestRecord[]>([]);
  const [emittingKey, setEmittingKey] = useState("");
  const [eventOptions, setEventOptions] = useState<string[]>([]);
  const [codexOptions, setCodexOptions] = useState<CodexRuntimeOptions>(EMPTY_CODEX_OPTIONS);
  const [revision, setRevision] = useState("");
  const [editing, setEditing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [token, setToken] = useState(getToken());

  const refreshOperationalData = useCallback(async () => {
    const [nextStatus, nextRuns, nextEvents, nextChangeRequests] = await Promise.all([
      api<RuntimeStatus>("/api/status"),
      api<RunSummary[]>("/api/runs?limit=100"),
      api<EventRecord[]>("/api/events?limit=50"),
      api<ChangeRequestRecord[]>("/api/change-requests?limit=100"),
    ]);
    setStatus(nextStatus);
    setRuns(nextRuns);
    setEvents(nextEvents);
    setChangeRequests(nextChangeRequests);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [config, options, nextCodexOptions] = await Promise.all([
        api<{ revision: string; document: ConfigDocument; error?: string }>("/api/config"),
        api<{ events: string[] }>("/api/options"),
        api<CodexRuntimeOptions>("/api/codex/runtime-options"),
        refreshOperationalData(),
      ]);
      const normalized = normalizeDocument(config.document);
      setDocument(normalized);
      setSavedDocument(structuredClone(normalized));
      setRevision(config.revision);
      setEventOptions(options.events);
      setCodexOptions(nextCodexOptions);
      setDirty(false);
      setEditing(false);
      if (config.error) setError(config.error);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [refreshOperationalData]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const timer = window.setInterval(() => { void refreshOperationalData().catch(() => undefined); }, 3000);
    return () => window.clearInterval(timer);
  }, [refreshOperationalData]);

  function changeDocument(next: ConfigDocument) {
    if (!editing) return;
    setDocument(next);
    setDirty(true);
    setNotice("");
  }

  function beginEditing() {
    if (!document) return;
    setSavedDocument(structuredClone(document));
    setEditing(true);
    setDirty(false);
    setNotice("已进入编辑模式，修改后请选择保存或取消");
  }

  function cancelEditing() {
    if (savedDocument) setDocument(structuredClone(savedDocument));
    setEditing(false);
    setDirty(false);
    setError("");
    setNotice("已取消编辑，未保存的修改已撤销");
    window.setTimeout(() => setNotice(""), 3000);
  }

  async function save() {
    if (!document) return;
    setSaving(true);
    setError("");
    try {
      const result = await api<{ revision: string; document: ConfigDocument }>("/api/config", {
        method: "PUT",
        body: JSON.stringify({ document }),
      });
      const normalized = normalizeDocument(result.document);
      setDocument(normalized);
      setSavedDocument(structuredClone(normalized));
      setRevision(result.revision);
      setCodexOptions(await api<CodexRuntimeOptions>("/api/codex/runtime-options"));
      setDirty(false);
      setEditing(false);
      setNotice("配置已校验、保存并通知后台热加载");
      window.setTimeout(() => setNotice(""), 3500);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function control(action: "scan" | "pause" | "resume") {
    try {
      await api(`/api/control/${action}`, { method: "POST" });
      await refreshOperationalData();
      setNotice(action === "scan" ? "已请求立即扫描" : action === "pause" ? "已暂停新扫描" : "已恢复扫描");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    }
  }

  async function emitDiscovered(item: ChangeRequestRecord) {
    const hasMatchingRule = Boolean(document?.rules.some((rule) => (
      rule.enabled !== false
      && rule.events.includes("change_request.discovered")
      && (!rule.repositories || rule.repositories.includes(item.repository_id))
    )));
    const impact = hasMatchingRule
      ? "补发后会立即按当前触发规则调度 Agent，可能执行规则允许的写操作。"
      : "当前没有匹配的首次发现规则，补发只会记录事件，不会运行 Agent。";
    if (!window.confirm(`确定为 ${item.repository_id} #${item.number} 补发首次发现事件吗？\n\n${impact}`)) return;

    setEmittingKey(item.snapshot_key);
    setError("");
    try {
      const result = await api<{ created: boolean; reason: string }>(
        `/api/change-requests/${encodeURIComponent(item.repository_id)}/${item.number}/emit-discovered`,
        { method: "POST" },
      );
      await refreshOperationalData();
      setNotice(result.reason);
      window.setTimeout(() => setNotice(""), 3500);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "补发首次发现事件失败");
    } finally {
      setEmittingKey("");
    }
  }

  const tabs = useMemo<Array<{ id: Tab; label: string; mark: string }>>(() => [
    { id: "overview", label: "运行概览", mark: "01" },
    { id: "repositories", label: "仓库", mark: "02" },
    { id: "environment", label: "全局环境", mark: "03" },
    { id: "skills", label: "SKILL", mark: "04" },
    { id: "agents", label: "Agent", mark: "05" },
    { id: "rules", label: "触发规则", mark: "06" },
    { id: "runtime", label: "运行时配置", mark: "07" },
    { id: "runs", label: "运行与日志", mark: "08" },
  ], []);
  const configurableTab = tab === "repositories" || tab === "environment" || tab === "skills" || tab === "agents" || tab === "rules" || tab === "runtime";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><div className="brand-mark">TR</div><div><strong>Teamwork</strong><span>Review Agents</span></div></div>
        <nav>{tabs.map((item) => <button key={item.id} className={tab === item.id ? "active" : ""} onClick={() => setTab(item.id)}><span>{item.mark}</span>{item.label}</button>)}</nav>
        <div className="sidebar-footer"><span className={`service-dot ${status.paused ? "paused" : status.running_cycle ? "busy" : ""}`} /><div><strong>{status.paused ? "已暂停" : "后台在线"}</strong><small>rev {shortRevision(revision || status.config_revision)}</small></div></div>
      </aside>
      <main className="main">
        <header className="topbar">
          <div><span className="eyebrow">MR / PR AUTOMATION</span><h1>{tabs.find((item) => item.id === tab)?.label}</h1></div>
          <div className="top-actions">
            <label className="token-field"><span>管理 Token</span><input type="password" value={token} placeholder="本机模式可留空" onChange={(event) => setToken(event.target.value)} onBlur={() => { persistToken(token); if (!editing) void load(); }} /></label>
            {(configurableTab || editing) && (
              editing ? (
                <div className="button-group edit-actions">
                  <button className="button secondary" disabled={saving} onClick={cancelEditing}>取消</button>
                  <button className="button primary" disabled={!dirty || saving} onClick={save}>{saving ? "保存中…" : "保存配置"}</button>
                </div>
              ) : (
                <button className="button primary" onClick={beginEditing}>编辑配置</button>
              )
            )}
          </div>
        </header>
        <div className="content">
          {error && <div className="alert error"><span>{error}</span><button onClick={() => setError("")}>×</button></div>}
          {notice && <div className="alert success">{notice}</div>}
          {loading && <div className="loading-screen"><span className="spinner" />正在连接后台服务…</div>}
          {!loading && document && (
            <>
              {tab === "overview" && (
                <Overview
                  status={status}
                  events={events}
                  changeRequests={changeRequests}
                  emittingKey={emittingKey}
                  onAction={control}
                  onEmitDiscovered={(item) => { void emitDiscovered(item); }}
                />
              )}
              {configurableTab && (
                <>
                  <div className={`edit-mode-banner ${editing ? "editing" : ""}`}>
                    <span>{editing ? "编辑模式" : "只读模式"}</span>
                    <small>{editing ? "修改会暂存在页面中，请使用右上角保存或取消。" : "点击右上角“编辑配置”后才能修改。"}</small>
                  </div>
                  <fieldset className="config-editor-surface" disabled={!editing}>
                    {tab === "repositories" && <RepositoriesEditor document={document} onChange={changeDocument} />}
                    {tab === "environment" && <GlobalEnvironment document={document} onChange={changeDocument} />}
                    {tab === "skills" && <SkillsEditor document={document} onChange={changeDocument} />}
                    {tab === "agents" && <AgentsEditor document={document} codexOptions={codexOptions} onChange={changeDocument} />}
                    {tab === "rules" && <RulesEditor document={document} events={eventOptions} onChange={changeDocument} />}
                    {tab === "runtime" && <CodexRuntimeEditor document={document} options={codexOptions} onChange={changeDocument} />}
                  </fieldset>
                  {tab === "runtime" && (
                    <CodexAccountCard
                      configuredHome={savedDocument?.runtime.codex_home ? String(savedDocument.runtime.codex_home) : undefined}
                      homeHasUnsavedChange={String(document.runtime.codex_home ?? "") !== String(savedDocument?.runtime.codex_home ?? "")}
                    />
                  )}
                  {tab === "environment" && <ConfigHistory />}
                </>
              )}
              {tab === "runs" && <RunsView runs={runs} onRefresh={() => { void refreshOperationalData(); }} />}
            </>
          )}
        </div>
      </main>
    </div>
  );
}
