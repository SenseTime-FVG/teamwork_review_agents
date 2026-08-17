import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  getToken,
  setToken as persistToken,
  streamRunLogs,
  uploadPromptFile,
} from "./api";
import type { ManagedPromptFile } from "./api";
import type {
  Agent,
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
} from "./types";

type Tab = "overview" | "repositories" | "environment" | "agents" | "rules" | "runs";

const EMPTY_STATUS: RuntimeStatus = {
  paused: false,
  running_cycle: false,
  config_revision: "",
  stats: { runs: {}, events: {} },
};

function normalizeDocument(value: Partial<ConfigDocument>): ConfigDocument {
  return {
    database: value.database ?? { path: "../data/teamwork-review-agents.db" },
    scanner: {
      interval_seconds: 60,
      max_pages: 2,
      page_size: 50,
      emit_initial_events: false,
      ...(value.scanner ?? {}),
    },
    runtime: {
      max_concurrent_agents: 4,
      lock_timeout_seconds: 300,
      lock_ttl_seconds: 120,
      max_sub_agent_depth: 2,
      max_agent_runs_per_root: 8,
      event_retry_count: 2,
      codex_binary: "codex",
      ...(value.runtime ?? {}),
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
    agents: value.agents ?? {},
    rules: value.rules ?? [],
  };
}

function timeText(timestamp?: number | null): string {
  return timestamp ? new Date(timestamp * 1000).toLocaleString("zh-CN") : "—";
}

function shortRevision(revision?: string): string {
  return revision ? revision.slice(0, 9) : "—";
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    running: "运行中",
    completed: "已完成",
    failed: "失败",
    timed_out: "超时",
    cancelled: "已取消",
    pending: "待处理",
    processing: "处理中",
  };
  return labels[status] ?? status;
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

function Toggle(props: { label: string; checked: boolean; onChange: (value: boolean) => void }) {
  return (
    <label className="toggle-row">
      <button
        type="button"
        role="switch"
        aria-checked={props.checked}
        className={`toggle ${props.checked ? "active" : ""}`}
        onClick={() => props.onChange(!props.checked)}
      >
        <span />
      </button>
      {props.label}
    </label>
  );
}

function MultiSelect(props: {
  label: string;
  values: string[];
  options: string[];
  onChange: (values: string[]) => void;
}) {
  return (
    <label className="field">
      <span>{props.label}</span>
      <select
        multiple
        value={props.values}
        onChange={(event) =>
          props.onChange(Array.from(event.target.selectedOptions, (option) => option.value))
        }
      >
        {props.options.map((option) => (
          <option key={option} value={option}>{option}</option>
        ))}
      </select>
      <small>按住 Command / Ctrl 可多选</small>
    </label>
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

function EnvironmentEditor(props: {
  title: string;
  value: EnvironmentMap;
  onChange: (value: EnvironmentMap) => void;
  compact?: boolean;
}) {
  const entries = Object.entries(props.value);

  function update(name: string, nextName: string, variable: EnvironmentVariable) {
    const next = { ...props.value };
    delete next[name];
    if (nextName) next[nextName] = variable;
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
          const variable = normalizeVariable(raw);
          const source = variable.from_system !== undefined ? "system" : "value";
          return (
            <div className="env-row" key={name}>
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
                  onChange={(event) => update(name, name, { ...variable, expose_to_prompt: event.target.checked })}
                />
                Prompt
              </label>
              <label className="mini-check">
                <input
                  type="checkbox"
                  checked={variable.expose_to_process ?? true}
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
    </section>
  );
}

function Overview(props: {
  status: RuntimeStatus;
  events: EventRecord[];
  onAction: (action: "scan" | "pause" | "resume") => void;
}) {
  const runTotal = Object.values(props.status.stats.runs).reduce((sum, value) => sum + value, 0);
  const eventTotal = Object.values(props.status.stats.events).reduce((sum, value) => sum + value, 0);
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
        <div className="metric-card"><span>Agent 运行</span><strong>{runTotal}</strong><small>{props.status.stats.runs.running ?? 0} 个正在执行</small></div>
        <div className="metric-card"><span>MR / PR 事件</span><strong>{eventTotal}</strong><small>{props.status.stats.events.pending ?? 0} 个待处理</small></div>
        <div className="metric-card"><span>最近周期</span><strong>{props.status.running_cycle ? "进行中" : "已结束"}</strong><small>{timeText(props.status.last_started_at)}</small></div>
        <div className="metric-card"><span>配置</span><strong>{shortRevision(props.status.config_revision)}</strong><small>{props.status.config_error ? "热加载失败" : "已生效"}</small></div>
      </div>
      <section className="section-card">
        <div className="section-title-row"><div><h2>最近事件</h2><p>扫描器生成的 MR / PR 语义变化</p></div></div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>事件</th><th>仓库</th><th>编号</th><th>状态</th><th>时间</th></tr></thead>
            <tbody>
              {props.events.map((event) => (
                <tr key={event.event_id}>
                  <td className="mono">{event.event_type}</td>
                  <td>{event.repository_id}</td>
                  <td>#{event.number}</td>
                  <td><StatusPill value={event.status} /></td>
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
        onChange={(global) => props.onChange({ ...props.document, environment: { global } })}
      />
      <section className="section-card">
        <div className="section-title-row"><div><h2>后台与扫描设置</h2><p>这些值保存后由常驻进程热加载。</p></div></div>
        <div className="form-grid three">
          <Field label="扫描间隔（秒）" type="number" value={Number(props.document.scanner.interval_seconds)} onChange={(value) => patchSection("scanner", "interval_seconds", Number(value))} />
          <Field label="最大页数" type="number" value={Number(props.document.scanner.max_pages)} onChange={(value) => patchSection("scanner", "max_pages", Number(value))} />
          <Field label="每页数量" type="number" value={Number(props.document.scanner.page_size)} onChange={(value) => patchSection("scanner", "page_size", Number(value))} />
          <Field label="配置检测（秒）" type="number" value={Number(props.document.web.config_poll_seconds)} onChange={(value) => patchSection("web", "config_poll_seconds", Number(value))} />
          <Field label="日志保留（天）" type="number" value={Number(props.document.web.log_retention_days)} onChange={(value) => patchSection("web", "log_retention_days", Number(value))} />
          <Field label="最大并行 Agent" type="number" value={Number(props.document.runtime.max_concurrent_agents)} onChange={(value) => patchSection("runtime", "max_concurrent_agents", Number(value))} />
          <Field label="监听地址" value={String(props.document.web.host)} onChange={(value) => patchSection("web", "host", value)} />
          <Field label="端口" type="number" value={Number(props.document.web.port)} onChange={(value) => patchSection("web", "port", Number(value))} />
          <Field label="管理员 Token 环境变量" value={String(props.document.web.admin_token_env ?? "")} onChange={(value) => patchSection("web", "admin_token_env", value || undefined)} help="非本机监听时必填" />
        </div>
        <div className="toggle-grid">
          <Toggle label="首次发现 MR / PR 时触发事件" checked={Boolean(props.document.scanner.emit_initial_events)} onChange={(value) => patchSection("scanner", "emit_initial_events", value)} />
        </div>
      </section>
    </div>
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
    repositories[index] = { ...repositories[index], id };
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
            <p>后台使用这里的平台 API 和 Token 扫描远端 MR / PR；它不是 Git clone 或 SSH 连接，也不会在此处克隆代码。</p>
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
            const referencedRepositories = props.document.repositories.filter((repository) => repository.provider === name).length;
            return (
              <div className="sub-card provider-row" key={name}>
                <CommitField label="连接名称" value={name} onCommit={(nextName) => renameProvider(name, nextName)} />
                <label className="field"><span>代码平台</span><select value={String(provider.kind)} onChange={(event) => {
                  const kind = event.target.value as "github" | "gitlab";
                  updateProvider(name, { kind, ...providerDefaults(kind) });
                }}><option value="github">GitHub</option><option value="gitlab">GitLab</option></select></label>
                <Field label="平台 API 地址" value={String(provider.base_url ?? "")} onChange={(value) => updateProvider(name, { base_url: value })} help="自建 GitHub Enterprise / GitLab 时改为实际 API 地址" />
                <Field label="Token 所在环境变量" value={String(provider.token_env ?? "")} onChange={(value) => updateProvider(name, { token_env: value })} help="这里只填变量名，例如 GITHUB_TOKEN，不填写真实 Token" />
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
          <div><h2>仓库</h2><p>选择上方的平台连接来扫描远端 MR / PR；本地工作目录则供 Codex CLI 读取或修改代码。</p></div>
          <button
            className="button primary"
            disabled={providerNames.length === 0}
            title={providerNames.length === 0 ? "请先添加 GitHub / GitLab 连接" : "添加仓库"}
            onClick={() => props.onChange({
              ...props.document,
              repositories: [...props.document.repositories, {
                id: `repository-${props.document.repositories.length + 1}`,
                provider: providerNames[0],
                project: "owner/repository",
                workspace: "./workspaces/repository",
                enabled: false,
                environment: {},
              }],
            })}
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
                <Field label="远端项目路径" value={repository.project} onChange={(project) => updateRepository(index, { project })} placeholder="group/project" help="GitHub 填 owner/repository，GitLab 填 group/project" />
                <Field label="本地 Git 工作目录" value={repository.workspace} onChange={(workspace) => updateRepository(index, { workspace })} help="需提前准备好代码，Agent 会在此目录运行 Codex CLI" />
              </div>
              <EnvironmentEditor compact title="仓库环境变量" value={repository.environment ?? {}} onChange={(environment) => updateRepository(index, { environment })} />
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}

function AgentsEditor(props: {
  document: ConfigDocument;
  onChange: (document: ConfigDocument) => void;
}) {
  const names = Object.keys(props.document.agents);
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
          props.onChange({ ...props.document, agents: { ...props.document.agents, [name]: { prompt: "请处理当前 MR / PR。", sandbox: "read-only", timeout_seconds: 1200, write_scopes: [], allowed_sub_agents: [], environment: {} } } });
        }}>+ 添加 Agent</button>
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
                <Field label="模型（可选）" value={agent.model ?? ""} onChange={(model) => update(name, { model: model || undefined })} placeholder="继承 Codex 默认模型" help="留空时使用 Codex CLI 当前默认模型" />
                <label className="field"><span>本地文件权限（Sandbox）</span><select value={agent.sandbox ?? "read-only"} onChange={(event) => {
                  const sandbox = event.target.value as Agent["sandbox"];
                  const nextScopes = sandbox === "read-only"
                    ? writeScopes.filter((scope) => scope !== "workspace")
                    : Array.from(new Set([...writeScopes, "workspace"]));
                  update(name, { sandbox, write_scopes: nextScopes as Agent["write_scopes"] });
                }}><option value="read-only">只读：不能修改本地文件</option><option value="workspace-write">工作区可写：可修改仓库文件</option><option value="danger-full-access">完全访问：高风险</option></select><small>切换为可写模式时会自动启用“本地仓库写操作”</small></label>
                <Field label="超时（秒）" type="number" value={agent.timeout_seconds ?? 1200} onChange={(value) => update(name, { timeout_seconds: Number(value) })} />
                <Field label="输出 Schema（可选）" value={agent.output_schema ?? ""} onChange={(output_schema) => update(name, { output_schema: output_schema || undefined })} />
              </div>
              <div className="permissions-grid">
                <ChoiceCards
                  title="写操作声明"
                  description="用于申请串行资源锁和记录权限边界，不等同于平台账号授权。"
                  values={writeScopes}
                  options={[
                    { value: "change_request", label: "MR / PR 写操作", description: "评论、标签、审批或合并时锁定当前变更请求" },
                    { value: "workspace", label: "本地仓库写操作", description: "修改代码、提交或推送时锁定当前工作目录" },
                  ]}
                  onChange={(values) => {
                    const hasWorkspace = values.includes("workspace");
                    update(name, {
                      write_scopes: values as Agent["write_scopes"],
                      sandbox: hasWorkspace
                        ? agent.sandbox === "danger-full-access" ? "danger-full-access" : "workspace-write"
                        : "read-only",
                    });
                  }}
                />
                <ChoiceCards
                  title="允许调用的 sub-agent"
                  description="只是授予 invoke_agent 委托权限，不会自动运行；MR 规则仍只触发当前 Agent。"
                  values={agent.allowed_sub_agents ?? []}
                  options={subAgentOptions}
                  emptyText="暂无其他 Agent。请先创建另一个 Agent，再回来授予调用权限。"
                  onChange={(allowed_sub_agents) => update(name, { allowed_sub_agents })}
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
              <EnvironmentEditor compact title="Agent 环境变量" value={agent.environment ?? {}} onChange={(environment) => update(name, { environment })} />
            </article>
          );
        })}
      </div>
    </section>
  );
}

function JsonEditor(props: { value: Record<string, unknown>; onChange: (value: Record<string, unknown>) => void }) {
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
      <span>条件（JSON）</span>
      <textarea className="mono" rows={7} value={text} onChange={(event) => setText(event.target.value)} onBlur={apply} />
      {error ? <small className="error-text">{error}</small> : <small>字段名可使用 __contains、__gte、__in 等操作符后缀</small>}
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
        <button className="button primary" onClick={() => props.onChange({ ...props.document, rules: [...props.document.rules, { name: `rule-${props.document.rules.length + 1}`, events: [props.events[0] ?? "change_request.updated"], agents: agentNames.slice(0, 1), conditions: {}, enabled: true }] })}>+ 添加规则</button>
      </div>
      <div className="card-list">
        {props.document.rules.map((rule, index) => (
          <article className="sub-card rule-card" key={`${rule.name}-${index}`}>
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

function RunsView(props: { runs: RunSummary[]; onRefresh: () => void }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [logs, setLogs] = useState<RunLog[]>([]);
  const [error, setError] = useState("");

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

  return (
    <div className="runs-layout">
      <section className="section-card runs-list">
        <div className="section-title-row"><div><h2>Agent 运行记录</h2><p>点击一条记录查看 Prompt、环境摘要和持续日志。</p></div><button className="button secondary" onClick={props.onRefresh}>刷新</button></div>
        <div className="run-items">
          {props.runs.map((run) => (
            <button key={run.run_id} className={`run-item ${selectedId === run.run_id ? "selected" : ""}`} onClick={() => setSelectedId(run.run_id)}>
              <span className="run-status-dot" data-status={run.status} />
              <span><strong>{run.agent_name}</strong><small>{run.resource_key}</small></span>
              <span><StatusPill value={run.status} /><small>{timeText(run.started_at)}</small></span>
            </button>
          ))}
          {props.runs.length === 0 && <div className="empty">尚无 Agent 运行记录</div>}
        </div>
      </section>
      <section className="section-card log-panel">
        {!selectedId && <div className="empty tall">选择左侧运行记录查看详情</div>}
        {selectedId && !detail && !error && <div className="empty tall">正在加载运行详情…</div>}
        {error && <div className="alert error">{error}</div>}
        {detail && (
          <>
            <div className="run-detail-head">
              <div><span className="eyebrow">{detail.run_id}</span><h2>{detail.agent_name}</h2><p>{detail.rule_name ?? "sub-agent 调用"} · 配置 {shortRevision(detail.config_revision)}</p></div>
              <StatusPill value={detail.status} />
            </div>
            <div className="detail-tabs">
              <details open><summary>实时日志 <span>{logs.length}</span></summary><div className="terminal">{logs.map((log) => <div key={log.id} className={`log-line stream-${log.stream}`}><time>{new Date(log.created_at * 1000).toLocaleTimeString("zh-CN")}</time><b>{log.event_type}</b><pre>{log.payload}</pre></div>)}{logs.length === 0 && <span className="terminal-empty">等待 Codex 输出…</span>}</div></details>
              <details><summary>最终消息</summary><pre className="detail-pre">{detail.final_message ?? detail.error ?? "暂无"}</pre></details>
              <details><summary>渲染后的 Prompt</summary><pre className="detail-pre">{detail.prompt}</pre></details>
              <details><summary>环境变量审计</summary><pre className="detail-pre">{JSON.stringify(detail.environment, null, 2)}</pre></details>
              {detail.children.length > 0 && <details><summary>Sub-agent <span>{detail.children.length}</span></summary><div className="children-list">{detail.children.map((child) => <button key={child.run_id} onClick={() => setSelectedId(child.run_id)}>{child.agent_name}<StatusPill value={child.status} /></button>)}</div></details>}
            </div>
          </>
        )}
      </section>
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("overview");
  const [document, setDocument] = useState<ConfigDocument | null>(null);
  const [savedDocument, setSavedDocument] = useState<ConfigDocument | null>(null);
  const [status, setStatus] = useState<RuntimeStatus>(EMPTY_STATUS);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [eventOptions, setEventOptions] = useState<string[]>([]);
  const [revision, setRevision] = useState("");
  const [editing, setEditing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [token, setToken] = useState(getToken());

  const refreshOperationalData = useCallback(async () => {
    const [nextStatus, nextRuns, nextEvents] = await Promise.all([
      api<RuntimeStatus>("/api/status"),
      api<RunSummary[]>("/api/runs?limit=100"),
      api<EventRecord[]>("/api/events?limit=50"),
    ]);
    setStatus(nextStatus);
    setRuns(nextRuns);
    setEvents(nextEvents);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [config, options] = await Promise.all([
        api<{ revision: string; document: ConfigDocument; error?: string }>("/api/config"),
        api<{ events: string[] }>("/api/options"),
        refreshOperationalData(),
      ]);
      const normalized = normalizeDocument(config.document);
      setDocument(normalized);
      setSavedDocument(structuredClone(normalized));
      setRevision(config.revision);
      setEventOptions(options.events);
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

  const tabs = useMemo<Array<{ id: Tab; label: string; mark: string }>>(() => [
    { id: "overview", label: "运行概览", mark: "01" },
    { id: "repositories", label: "仓库", mark: "02" },
    { id: "environment", label: "全局环境", mark: "03" },
    { id: "agents", label: "Agent", mark: "04" },
    { id: "rules", label: "触发规则", mark: "05" },
    { id: "runs", label: "运行与日志", mark: "06" },
  ], []);
  const configurableTab = tab === "repositories" || tab === "environment" || tab === "agents" || tab === "rules";

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
              {tab === "overview" && <Overview status={status} events={events} onAction={control} />}
              {configurableTab && (
                <>
                  <div className={`edit-mode-banner ${editing ? "editing" : ""}`}>
                    <span>{editing ? "编辑模式" : "只读模式"}</span>
                    <small>{editing ? "修改会暂存在页面中，请使用右上角保存或取消。" : "点击右上角“编辑配置”后才能修改。"}</small>
                  </div>
                  <fieldset className="config-editor-surface" disabled={!editing}>
                    {tab === "repositories" && <RepositoriesEditor document={document} onChange={changeDocument} />}
                    {tab === "environment" && <GlobalEnvironment document={document} onChange={changeDocument} />}
                    {tab === "agents" && <AgentsEditor document={document} onChange={changeDocument} />}
                    {tab === "rules" && <RulesEditor document={document} events={eventOptions} onChange={changeDocument} />}
                  </fieldset>
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
