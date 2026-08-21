import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  api,
  getToken,
  setToken as persistToken,
  streamPreflightLogs,
  streamRunLogs,
  uploadPromptFile,
  uploadSkillDirectory,
} from "./api";
import type { ManagedPromptFile, ManagedSkillDirectory } from "./api";
import { MarkdownMessage, RunMessageFeed } from "./RunMessageFeed";
import { presentRunLogs } from "./runLogPresentation";
import type {
  Agent,
  ChangeRequestDetailRecord,
  ChangeRequestRecord,
  CodexAccountStatus,
  CodexConnectionTestResult,
  CodexInheritedSetting,
  CodexLoginSession,
  CodexRuntimeConfig,
  CodexRuntimeOptions,
  ConfigDocument,
  EnvironmentMap,
  EnvironmentVariable,
  EventAgentRunSummary,
  EventDetailRecord,
  EventDispatchDetail,
  EventRecord,
  ManualLatestEventBatchResponse,
  ModelProviderConfig,
  ModelProviderDriver,
  ModelProviderSnapshot,
  PreflightRunDetail,
  PreflightRunSummary,
  Repository,
  RepositoryAgentWorkspace,
  RepositoryAgentWorkspacePrepareStep,
  RepositoryGitDetail,
  RepositoryPreflight,
  RepositoryPreflightStep,
  RepositoryWorkspaceStatus,
  RepositoryWorkspaceWarmupStatus,
  Rule,
  RunDetail,
  RunLog,
  RunSummary,
  RuntimeStatus,
  Skill,
} from "./types";

type Tab = "overview" | "repositories" | "environment" | "model-providers" | "skills" | "agents" | "rules" | "runs";

type OverviewLimit = number | null;

type OverviewFilter = {
  repositoryId: string;
  number: string;
  status: string;
  statuses: string[];
  limit: OverviewLimit;
  page: number;
};

type OverviewPage = {
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
};

type PaginatedOverviewResponse<T> = OverviewPage & {
  items: T[];
};

type ExecutionTypeFilter = "all" | "agent" | "preflight";

type ExecutionStatusFilter = "waiting" | "running" | "success" | "failure" | "timed_out" | "cancelled";

type ExecutionFilter = {
  repositoryId: string;
  number: string;
  type: ExecutionTypeFilter;
  statuses: ExecutionStatusFilter[];
  limit: number | null;
};

type OverviewConfirmation = {
  kind: "discovered" | "latest" | "latest-batch";
  items: ChangeRequestRecord[];
  eyebrow: string;
  title: string;
  description: string;
  details: Array<{ label: string; value: string; mono?: boolean }>;
  impactTitle: string;
  impact: string;
  impactTone: "attention" | "quiet";
  safetyNote: string;
  confirmLabel: string;
};

type AgentActionConfirmation = {
  eyebrow?: string;
  title: string;
  description: string;
  details: Array<{ label: string; value: string; mono?: boolean }>;
  impactTitle: string;
  impact: string;
  confirmLabel: string;
  dangerous?: boolean;
};

const DEFAULT_OVERVIEW_FILTER: OverviewFilter = {
  repositoryId: "",
  number: "",
  status: "",
  statuses: [],
  limit: 10,
  page: 1,
};

const EMPTY_OVERVIEW_PAGE: OverviewPage = {
  total: 0,
  page: 1,
  page_size: 10,
  total_pages: 1,
};

const DEFAULT_EXECUTION_FILTER: ExecutionFilter = {
  repositoryId: "",
  number: "",
  type: "all",
  statuses: [],
  limit: 20,
};

const EXECUTION_STATUS_OPTIONS: Array<{
  value: ExecutionStatusFilter;
  label: string;
}> = [
  { value: "waiting", label: "等待中" },
  { value: "running", label: "执行中" },
  { value: "success", label: "成功" },
  { value: "failure", label: "失败" },
  { value: "timed_out", label: "超时" },
  { value: "cancelled", label: "已取消" },
];

const CHANGE_REQUEST_STATUS_OPTIONS = [
  { value: "opened", label: "打开" },
  { value: "closed", label: "已关闭" },
  { value: "merged", label: "已合并" },
];

const EVENT_STATUS_OPTIONS = [
  { value: "pending", label: "待处理" },
  { value: "processing", label: "规则匹配中" },
  { value: "unmatched", label: "未触发" },
  { value: "triggered", label: "已触发" },
  { value: "completed", label: "已处理" },
  { value: "failed", label: "处理失败" },
  { value: "cancelled", label: "已取消" },
];

let bodyScrollLockCount = 0;
let bodyScrollOriginalOverflow: string | null = null;

function acquireBodyScrollLock(): () => void {
  if (bodyScrollLockCount === 0) {
    bodyScrollOriginalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
  }
  bodyScrollLockCount += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    bodyScrollLockCount = Math.max(0, bodyScrollLockCount - 1);
    if (bodyScrollLockCount === 0) {
      document.body.style.overflow = bodyScrollOriginalOverflow ?? "";
      bodyScrollOriginalOverflow = null;
    }
  };
}

function useBodyScrollLock(active: boolean): void {
  useEffect(() => {
    if (!active) return undefined;
    return acquireBodyScrollLock();
  }, [active]);
}

const EMPTY_STATUS: RuntimeStatus = {
  paused: false,
  running_cycle: false,
  dispatching_events: false,
  config_revision: "",
  stats: { runs: {}, events: {}, change_requests: {} },
};

const EMPTY_CODEX_OPTIONS: CodexRuntimeOptions = {
  models: [],
  catalog_source: "unavailable",
  inherited_model: {
    value: null,
    source: "builtin",
    label: "继承 Codex CLI / 账号默认（UNK）",
  },
  codex_model: null,
  codex_model_source: "builtin",
  inherited_settings: {
    model_reasoning_effort: { value: null, source: "unknown", known: false },
    fast_mode: { value: null, source: "unknown", known: false },
    model_verbosity: { value: null, source: "unknown", known: false },
    personality: { value: null, source: "unknown", known: false },
    web_search: { value: null, source: "unknown", known: false },
  },
  user_model: null,
  user_config_path: "~/.codex/config.toml",
  codex_home: "~/.codex",
  binary: {},
  model_cache: { path: "~/.codex/models_cache.json" },
  user_mcp_servers: [],
  managed_sandbox: {
    enabled: true,
    fail_closed: true,
    available: false,
    platform: "未知",
    backend: null,
    error: "尚未读取运行时能力",
  },
};

async function loadCodexRuntimeOptions(): Promise<{
  options: CodexRuntimeOptions;
  error: string;
}> {
  try {
    return {
      options: await api<CodexRuntimeOptions>("/api/codex/runtime-options"),
      error: "",
    };
  } catch (reason) {
    return {
      options: EMPTY_CODEX_OPTIONS,
      error: `Codex 运行时诊断不可用：${reason instanceof Error ? reason.message : "加载失败"}`,
    };
  }
}

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
  const modelProviders = value.model_providers ?? {
    "codex-cli": {
      display_name: "Codex CLI",
      driver: "codex_cli" as const,
      enabled: true,
    },
  };
  return {
    database: value.database ?? { path: "../data/teamwork-review-agents.db" },
    scanner: {
      interval_seconds: 300,
      max_items_per_repository: maxItems,
      emit_initial_events: false,
      ...scannerInput,
    },
    runtime: {
      max_concurrent_agents: 5,
      agent_concurrency_limit: 5,
      lock_timeout_seconds: 300,
      lock_ttl_seconds: 120,
      max_sub_agent_depth: 2,
      max_agent_runs_per_root: 8,
      event_retry_count: 2,
      worktree_retention_days: 7,
      codex_binary: "codex",
      inherit_user_mcp_servers: false,
      allowed_user_mcp_servers: [],
      repository_initialization_timeout_seconds: 1800,
      git_timeout_seconds: 600,
      agent_idle_timeout_seconds: 300,
      ...runtimeInput,
      managed_sandbox: {
        enabled: true,
        fail_closed: true,
        ...(runtimeInput.managed_sandbox ?? {}),
      },
      default_model: runtimeInput.default_model ?? {
        provider: "codex-cli",
      },
      codex: {
        execution_mode: "model",
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
    model_providers: modelProviders,
    repositories: value.repositories ?? [],
    skills: value.skills ?? {},
    agents: value.agents ?? {},
    rules: value.rules ?? [],
  };
}

function createEmptyAgent(): Agent {
  return {
    prompt: "请处理当前 MR / PR。",
    sandbox: "read-only",
    home_mode: "inherit",
    network_access: false,
    network_domains: [],
    timeout_seconds: 1200,
    write_scopes: [],
    managed_comment: false,
    managed_comment_model_signature: false,
    allowed_sub_agents: [],
    skills: [],
    environment: {},
  };
}

function timeText(timestamp?: number | null): string {
  return timestamp ? new Date(timestamp * 1000).toLocaleString("zh-CN") : "—";
}

function dateTimeText(value?: string | null): string {
  return value ? new Date(value).toLocaleString("zh-CN") : "—";
}

function overviewQuery(filter: OverviewFilter, includeNumber = false): string {
  const parameters = new URLSearchParams();
  parameters.set("page", String(filter.page));
  if (filter.limit === null) {
    parameters.set("all_records", "true");
  } else {
    parameters.set("limit", String(filter.limit));
  }
  if (filter.repositoryId) parameters.set("repository_id", filter.repositoryId);
  if (includeNumber && /^\d+$/.test(filter.number) && Number(filter.number) > 0) {
    parameters.set("number", filter.number);
  }
  if (filter.statuses.length > 0) {
    filter.statuses.forEach((status) => parameters.append("status", status));
  } else if (filter.status) {
    parameters.set("status", filter.status);
  }
  return parameters.toString();
}

function executionQuery(filter: ExecutionFilter): string {
  const parameters = new URLSearchParams();
  if (filter.limit === null) {
    parameters.set("all_records", "true");
  } else {
    parameters.set("limit", String(filter.limit));
  }
  if (filter.repositoryId) parameters.set("repository_id", filter.repositoryId);
  if (/^\d+$/.test(filter.number) && Number(filter.number) > 0) {
    parameters.set("number", filter.number);
  }
  filter.statuses.forEach((status) => parameters.append("status_group", status));
  return parameters.toString();
}

function executionStatusMatches(
  kind: "agent" | "preflight",
  status: string,
  filters: ExecutionStatusFilter[],
): boolean {
  if (filters.length === 0) return true;
  const groups: Record<ExecutionStatusFilter, string[]> = kind === "agent"
    ? {
        waiting: ["queued", "preparing"],
        running: ["running"],
        success: ["completed"],
        failure: ["failed"],
        timed_out: ["timed_out"],
        cancelled: ["cancelled"],
      }
    : {
        waiting: [],
        running: ["running"],
        success: ["success"],
        failure: ["failure", "error"],
        timed_out: ["timed_out"],
        cancelled: ["cancelled"],
      };
  return filters.some((filter) => groups[filter].includes(status));
}

function shortRevision(revision?: string): string {
  return revision ? revision.slice(0, 9) : "—";
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "排队中",
    preparing: "准备工作区",
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

type SelectOption = { value: string; label: string };

function SelectControl(props: {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  ariaLabel?: string;
  ariaLabelledBy?: string;
  className?: string;
  disabled?: boolean;
}) {
  const controlId = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [openUpward, setOpenUpward] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const listboxId = `${controlId}-options`;
  const selectedIndex = props.options.findIndex((option) => option.value === props.value);
  const selectedOption = selectedIndex >= 0 ? props.options[selectedIndex] : props.options[0];

  useEffect(() => {
    if (props.disabled) setOpen(false);
  }, [props.disabled]);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnOutsideClick = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", closeOnOutsideClick);
    return () => document.removeEventListener("pointerdown", closeOnOutsideClick);
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return undefined;
    const updatePlacement = () => {
      const trigger = triggerRef.current;
      const menu = menuRef.current;
      if (!trigger || !menu) return;
      const bounds = trigger.getBoundingClientRect();
      const spaceAbove = bounds.top;
      const spaceBelow = window.innerHeight - bounds.bottom;
      setOpenUpward(spaceBelow < menu.offsetHeight + 8 && spaceAbove > spaceBelow);
    };
    updatePlacement();
    window.addEventListener("resize", updatePlacement);
    window.addEventListener("scroll", updatePlacement, true);
    return () => {
      window.removeEventListener("resize", updatePlacement);
      window.removeEventListener("scroll", updatePlacement, true);
    };
  }, [open, props.options.length]);

  useEffect(() => {
    if (!open || activeIndex < 0) return;
    document.getElementById(`${controlId}-option-${activeIndex}`)?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, controlId, open]);

  function openOptions(direction: "first" | "last" = "first") {
    if (props.disabled || !props.options.length) return;
    const fallbackIndex = direction === "last" ? props.options.length - 1 : 0;
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : fallbackIndex);
    setOpen(true);
  }

  function selectOption(index: number) {
    const option = props.options[index];
    if (!option) return;
    props.onChange(option.value);
    setOpen(false);
    triggerRef.current?.focus();
  }

  return (
    <div className={`select-combobox ${open ? "open" : ""} ${props.className ?? ""}`.trim()} ref={containerRef}>
      <button
        ref={triggerRef}
        className="select-combobox-trigger"
        type="button"
        role="combobox"
        aria-label={props.ariaLabel}
        aria-labelledby={props.ariaLabelledBy}
        aria-expanded={open}
        aria-controls={listboxId}
        aria-haspopup="listbox"
        aria-activedescendant={open && activeIndex >= 0 ? `${controlId}-option-${activeIndex}` : undefined}
        disabled={props.disabled}
        onClick={() => {
          if (open) setOpen(false);
          else openOptions();
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            setOpen(false);
            return;
          }
          if (event.key === "Tab") {
            setOpen(false);
            return;
          }
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            if (!open) {
              openOptions(event.key === "ArrowUp" ? "last" : "first");
              return;
            }
            const offset = event.key === "ArrowDown" ? 1 : -1;
            setActiveIndex((current) => (
              current < 0
                ? 0
                : (current + offset + props.options.length) % props.options.length
            ));
            return;
          }
          if (open && event.key === "Home") {
            event.preventDefault();
            setActiveIndex(0);
            return;
          }
          if (open && event.key === "End") {
            event.preventDefault();
            setActiveIndex(props.options.length - 1);
            return;
          }
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            if (open) selectOption(activeIndex);
            else openOptions();
          }
        }}
      >
        <span>{selectedOption?.label ?? props.value}</span>
        <span className="select-combobox-chevron" aria-hidden="true">⌄</span>
      </button>
      {open && (
        <div
          ref={menuRef}
          id={listboxId}
          className={`select-combobox-options ${openUpward ? "open-upward" : ""}`}
          role="listbox"
          aria-label={props.ariaLabel}
          aria-labelledby={props.ariaLabelledBy}
        >
          {props.options.map((option, index) => (
            <button
              id={`${controlId}-option-${index}`}
              key={option.value}
              type="button"
              role="option"
              aria-selected={option.value === props.value}
              className={`select-combobox-option ${index === activeIndex ? "active" : ""} ${option.value === props.value ? "selected" : ""}`}
              onMouseDown={(event) => event.preventDefault()}
              onMouseEnter={() => setActiveIndex(index)}
              onClick={() => selectOption(index)}
            >
              <span className="select-combobox-check" aria-hidden="true">{option.value === props.value ? "✓" : ""}</span>
              <span>{option.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function MultiSelectControl(props: {
  values: string[];
  onChange: (values: string[]) => void;
  options: SelectOption[];
  allLabel: string;
  ariaLabel?: string;
  ariaLabelledBy?: string;
  className?: string;
  disabled?: boolean;
}) {
  const controlId = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [openUpward, setOpenUpward] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const listboxId = `${controlId}-options`;
  const menuOptions = useMemo(
    () => [{ value: "", label: props.allLabel }, ...props.options],
    [props.allLabel, props.options],
  );
  const selectedLabels = props.options
    .filter((option) => props.values.includes(option.value))
    .map((option) => option.label);
  const triggerLabel = selectedLabels.length > 0
    ? selectedLabels.join("、")
    : props.allLabel;

  useEffect(() => {
    if (props.disabled) setOpen(false);
  }, [props.disabled]);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnOutsideClick = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", closeOnOutsideClick);
    return () => document.removeEventListener("pointerdown", closeOnOutsideClick);
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return undefined;
    const updatePlacement = () => {
      const trigger = triggerRef.current;
      const menu = menuRef.current;
      if (!trigger || !menu) return;
      const bounds = trigger.getBoundingClientRect();
      const spaceAbove = bounds.top;
      const spaceBelow = window.innerHeight - bounds.bottom;
      setOpenUpward(spaceBelow < menu.offsetHeight + 8 && spaceAbove > spaceBelow);
    };
    updatePlacement();
    window.addEventListener("resize", updatePlacement);
    window.addEventListener("scroll", updatePlacement, true);
    return () => {
      window.removeEventListener("resize", updatePlacement);
      window.removeEventListener("scroll", updatePlacement, true);
    };
  }, [menuOptions.length, open]);

  useEffect(() => {
    if (!open || activeIndex < 0) return;
    document.getElementById(`${controlId}-option-${activeIndex}`)?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, controlId, open]);

  function isSelected(value: string): boolean {
    return value ? props.values.includes(value) : props.values.length === 0;
  }

  function openOptions(direction: "first" | "last" = "first") {
    if (props.disabled || !menuOptions.length) return;
    const selectedIndex = menuOptions.findIndex((option) => isSelected(option.value));
    const fallbackIndex = direction === "last" ? menuOptions.length - 1 : 0;
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : fallbackIndex);
    setOpen(true);
  }

  function toggleOption(index: number) {
    const option = menuOptions[index];
    if (!option) return;
    if (!option.value) {
      props.onChange([]);
      return;
    }
    props.onChange(
      props.values.includes(option.value)
        ? props.values.filter((value) => value !== option.value)
        : [...props.values, option.value],
    );
  }

  return (
    <div className={`select-combobox ${open ? "open" : ""} ${props.className ?? ""}`.trim()} ref={containerRef}>
      <button
        ref={triggerRef}
        className="select-combobox-trigger"
        type="button"
        role="combobox"
        aria-label={props.ariaLabel}
        aria-labelledby={props.ariaLabelledBy}
        aria-expanded={open}
        aria-controls={listboxId}
        aria-haspopup="listbox"
        aria-activedescendant={open && activeIndex >= 0 ? `${controlId}-option-${activeIndex}` : undefined}
        disabled={props.disabled}
        onClick={() => {
          if (open) setOpen(false);
          else openOptions();
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape" || event.key === "Tab") {
            setOpen(false);
            return;
          }
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            if (!open) {
              openOptions(event.key === "ArrowUp" ? "last" : "first");
              return;
            }
            const offset = event.key === "ArrowDown" ? 1 : -1;
            setActiveIndex((current) => (
              current < 0
                ? 0
                : (current + offset + menuOptions.length) % menuOptions.length
            ));
            return;
          }
          if (open && event.key === "Home") {
            event.preventDefault();
            setActiveIndex(0);
            return;
          }
          if (open && event.key === "End") {
            event.preventDefault();
            setActiveIndex(menuOptions.length - 1);
            return;
          }
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            if (open) toggleOption(activeIndex);
            else openOptions();
          }
        }}
      >
        <span>{triggerLabel}</span>
        <span className="select-combobox-chevron" aria-hidden="true">⌄</span>
      </button>
      {open && (
        <div
          ref={menuRef}
          id={listboxId}
          className={`select-combobox-options ${openUpward ? "open-upward" : ""}`}
          role="listbox"
          aria-multiselectable="true"
          aria-label={props.ariaLabel}
          aria-labelledby={props.ariaLabelledBy}
        >
          {menuOptions.map((option, index) => {
            const selected = isSelected(option.value);
            return (
              <button
                id={`${controlId}-option-${index}`}
                key={option.value || "__all__"}
                type="button"
                role="option"
                aria-selected={selected}
                className={`select-combobox-option ${index === activeIndex ? "active" : ""} ${selected ? "selected" : ""}`}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => toggleOption(index)}
              >
                <span className="select-combobox-check" aria-hidden="true">{selected ? "✓" : ""}</span>
                <span>{option.label}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function SelectField(props: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  help?: string;
  className?: string;
  disabled?: boolean;
}) {
  const fieldId = useId();

  return (
    <div className={`field select-field ${props.className ?? ""}`.trim()}>
      <span id={`${fieldId}-label`}>{props.label}</span>
      <SelectControl
        value={props.value}
        onChange={props.onChange}
        options={props.options}
        ariaLabelledBy={`${fieldId}-label`}
        disabled={props.disabled}
      />
      {props.help && <small>{props.help}</small>}
    </div>
  );
}

function MultiSelectField(props: {
  label: string;
  values: string[];
  onChange: (values: string[]) => void;
  options: SelectOption[];
  allLabel: string;
  className?: string;
  disabled?: boolean;
}) {
  const fieldId = useId();

  return (
    <div className={`field select-field ${props.className ?? ""}`.trim()}>
      <span id={`${fieldId}-label`}>{props.label}</span>
      <MultiSelectControl
        values={props.values}
        onChange={props.onChange}
        options={props.options}
        allLabel={props.allLabel}
        ariaLabelledBy={`${fieldId}-label`}
        disabled={props.disabled}
      />
    </div>
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
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(-1);
  const listboxId = `${props.id}-options`;
  const filteredModels = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return props.models;
    return props.models.filter((model) => (
      model.slug.toLowerCase().includes(normalized)
      || model.display_name.toLowerCase().includes(normalized)
    ));
  }, [props.models, query]);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnOutsideClick = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpen(false);
        setQuery("");
      }
    };
    document.addEventListener("pointerdown", closeOnOutsideClick);
    return () => document.removeEventListener("pointerdown", closeOnOutsideClick);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const currentIndex = filteredModels.findIndex((model) => model.slug === props.value);
    setActiveIndex(currentIndex >= 0 ? currentIndex : filteredModels.length ? 0 : -1);
  }, [filteredModels, open, props.value]);

  useEffect(() => {
    if (!open || activeIndex < 0) return;
    document.getElementById(`${props.id}-option-${activeIndex}`)?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, open, props.id]);

  function openAllModels() {
    setQuery("");
    setOpen(true);
  }

  function selectModel(model: CodexRuntimeOptions["models"][number]) {
    props.onChange(model.slug);
    setOpen(false);
    setQuery("");
  }

  return (
    <div className="field model-field" ref={containerRef}>
      <span id={`${props.id}-label`}>{props.label}</span>
      <div className={`model-combobox ${open ? "open" : ""}`}>
        <input
          ref={inputRef}
          id={props.id}
          value={props.value}
          placeholder={props.placeholder}
          role="combobox"
          aria-labelledby={`${props.id}-label`}
          aria-expanded={open}
          aria-controls={listboxId}
          aria-autocomplete="list"
          aria-activedescendant={open && activeIndex >= 0 ? `${props.id}-option-${activeIndex}` : undefined}
          autoComplete="off"
          onFocus={openAllModels}
          onClick={() => {
            if (!open) openAllModels();
          }}
          onChange={(event) => {
            props.onChange(event.target.value);
            setQuery(event.target.value);
            setOpen(true);
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              setOpen(false);
              setQuery("");
              return;
            }
            if (event.key === "Tab") {
              setOpen(false);
              setQuery("");
              return;
            }
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
              event.preventDefault();
              if (!open) {
                openAllModels();
                return;
              }
              if (!filteredModels.length) return;
              const offset = event.key === "ArrowDown" ? 1 : -1;
              setActiveIndex((current) => (
                current < 0
                  ? 0
                  : (current + offset + filteredModels.length) % filteredModels.length
              ));
              return;
            }
            if (event.key === "Enter" && open && activeIndex >= 0) {
              event.preventDefault();
              const model = filteredModels[activeIndex];
              if (model) selectModel(model);
            }
          }}
        />
        <button
          className="model-combobox-toggle"
          type="button"
          aria-label={open ? "关闭模型候选" : "展开全部模型候选"}
          aria-expanded={open}
          aria-controls={listboxId}
          onClick={() => {
            if (open) {
              setOpen(false);
              setQuery("");
            } else {
              openAllModels();
              inputRef.current?.focus();
            }
          }}
        ><span aria-hidden="true">⌄</span></button>
        {open && (
          <div className="model-combobox-options" id={listboxId} role="listbox" aria-labelledby={`${props.id}-label`}>
            {filteredModels.length ? filteredModels.map((model, index) => (
              <button
                id={`${props.id}-option-${index}`}
                key={model.slug}
                type="button"
                role="option"
                aria-selected={model.slug === props.value}
                className={`model-combobox-option ${index === activeIndex ? "active" : ""} ${model.slug === props.value ? "selected" : ""}`}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => selectModel(model)}
              >
                <span>{model.display_name}</span>
                <small>{model.slug}</small>
              </button>
            )) : (
              <div className="model-combobox-empty">
                {props.models.length ? "没有匹配模型，可继续手工填写模型 ID" : "没有可用候选，可继续手工填写模型 ID"}
              </div>
            )}
          </div>
        )}
      </div>
      {props.help && <small>{props.help}</small>}
    </div>
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
        <SelectControl
          value=""
          ariaLabel="选择已有 Prompt"
          className="prompt-file-select"
          options={[
            { value: "", label: "选择已有 Prompt…" },
            ...files.map((file) => ({ value: file.path, label: file.name })),
          ]}
          onChange={(value) => {
            if (value) props.onChange(value);
          }}
        />
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
              <SelectControl
                value={source}
                ariaLabel="变量来源"
                options={[
                  { value: "value", label: "固定值" },
                  { value: "system", label: "宿主机环境" },
                ]}
                onChange={(value) => {
                  const fromSystem = value === "system";
                  update(name, name, {
                    ...variable,
                    value: fromSystem ? undefined : "",
                    from_system: fromSystem ? name : undefined,
                  });
                }}
              />
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

function OverviewListControls(props: {
  repositories: Repository[];
  filter: OverviewFilter;
  statuses: Array<{ value: string; label: string }>;
  multiStatus?: boolean;
  showNumber?: boolean;
  onChange: (filter: OverviewFilter) => void;
}) {
  const predefinedLimits = [10, 20, 50];
  const limitMode = props.filter.limit === null
    ? "all"
    : predefinedLimits.includes(props.filter.limit)
      ? String(props.filter.limit)
      : "custom";
  const [customLimit, setCustomLimit] = useState(
    props.filter.limit !== null && !predefinedLimits.includes(props.filter.limit)
      ? String(props.filter.limit)
      : "100",
  );

  useEffect(() => {
    if (props.filter.limit !== null && !predefinedLimits.includes(props.filter.limit)) {
      setCustomLimit(String(props.filter.limit));
    }
  }, [props.filter.limit]);

  function applyCustomLimit() {
    const parsed = Number(customLimit);
    if (Number.isInteger(parsed) && parsed > 0) {
      props.onChange({ ...props.filter, limit: parsed });
      return;
    }
    const fallback = props.filter.limit !== null && props.filter.limit > 0
      ? props.filter.limit
      : 100;
    setCustomLimit(String(fallback));
  }

  const controlClassName = [
    "overview-list-controls",
    props.showNumber ? "overview-list-controls-numbered" : "",
    limitMode === "custom" ? "overview-list-controls-custom-limit" : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={controlClassName}>
      <SelectField
        className="overview-repository-filter"
        label="仓库"
        value={props.filter.repositoryId}
        onChange={(repositoryId) => props.onChange({ ...props.filter, repositoryId })}
        options={[
          { value: "", label: "全部仓库" },
          ...props.repositories.map((repository) => ({
            value: repository.id,
            label: `${repository.id} · ${repository.project}`,
          })),
        ]}
      />
      {props.showNumber && (
        <label className="overview-number-filter">
          <span>编号</span>
          <input
            type="number"
            min="1"
            step="1"
            inputMode="numeric"
            placeholder="全部"
            value={props.filter.number}
            onChange={(event) => props.onChange({ ...props.filter, number: event.target.value })}
          />
        </label>
      )}
      {props.multiStatus ? (
        <MultiSelectField
          label="状态"
          values={props.filter.statuses}
          onChange={(statuses) => props.onChange({ ...props.filter, status: "", statuses })}
          options={props.statuses}
          allLabel="全部状态"
        />
      ) : (
        <SelectField
          label="状态"
          value={props.filter.status}
          onChange={(status) => props.onChange({ ...props.filter, status, statuses: [] })}
          options={[
            { value: "", label: "全部状态" },
            ...props.statuses,
          ]}
        />
      )}
      <SelectField
        label="展示"
        value={limitMode}
        onChange={(value) => {
          if (value === "all") {
            props.onChange({ ...props.filter, limit: null });
          } else if (value === "custom") {
            const parsed = Number(customLimit);
            props.onChange({
              ...props.filter,
              limit: Number.isInteger(parsed) && parsed > 0 ? parsed : 100,
            });
          } else {
            props.onChange({ ...props.filter, limit: Number(value) });
          }
        }}
        options={[
          { value: "10", label: "10 条" },
          { value: "20", label: "20 条" },
          { value: "50", label: "50 条" },
          { value: "all", label: "全部" },
          { value: "custom", label: "自定义" },
        ]}
      />
      {limitMode === "custom" && (
        <label className="overview-custom-limit">
          <span>自定义条数</span>
          <input
            type="number"
            min="1"
            step="1"
            inputMode="numeric"
            value={customLimit}
            onChange={(event) => setCustomLimit(event.target.value)}
            onBlur={applyCustomLimit}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                applyCustomLimit();
                event.currentTarget.blur();
              }
            }}
          />
        </label>
      )}
    </div>
  );
}

function OverviewPagination(props: {
  page: OverviewPage;
  onPageChange: (page: number) => void;
}) {
  const { page, onPageChange } = props;
  return (
    <nav className="overview-pagination" aria-label="列表分页">
      <span>共 {page.total} 条</span>
      <div className="overview-pagination-actions">
        <button
          type="button"
          className="button secondary compact"
          disabled={page.page <= 1}
          onClick={() => onPageChange(page.page - 1)}
        >
          上一页
        </button>
        <span>第 {page.page} / {page.total_pages} 页</span>
        <button
          type="button"
          className="button secondary compact"
          disabled={page.page >= page.total_pages}
          onClick={() => onPageChange(page.page + 1)}
        >
          下一页
        </button>
      </div>
    </nav>
  );
}

function OverviewConfirmationDialog(props: {
  model: OverviewConfirmation | null;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { model, busy, onCancel } = props;

  useBodyScrollLock(model !== null);

  useEffect(() => {
    if (!model) return undefined;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [busy, model, onCancel]);

  if (!model) return null;
  return (
    <div className="overview-confirmation-layer">
      <button
        type="button"
        className="overview-confirmation-backdrop"
        aria-label="取消当前操作"
        disabled={busy}
        onClick={onCancel}
      />
      <section
        className="overview-confirmation"
        role="dialog"
        aria-modal="true"
        aria-labelledby="overview-confirmation-title"
        aria-describedby="overview-confirmation-description"
      >
        <header className="overview-confirmation-head">
          <div
            className={`overview-confirmation-icon ${model.kind === "discovered" ? "discovered" : "latest"}`}
            aria-hidden="true"
          >
            {model.kind === "discovered" ? "+" : "↗"}
          </div>
          <div>
            <span className="eyebrow">{model.eyebrow}</span>
            <h2 id="overview-confirmation-title">{model.title}</h2>
            <p id="overview-confirmation-description">{model.description}</p>
          </div>
          <button
            type="button"
            className="overview-confirmation-close"
            aria-label="关闭确认弹窗"
            disabled={busy}
            onClick={onCancel}
          >
            ×
          </button>
        </header>

        <dl className="overview-confirmation-details">
          {model.details.map((detail) => (
            <div key={detail.label}>
              <dt>{detail.label}</dt>
              <dd className={detail.mono ? "mono" : ""}>{detail.value}</dd>
            </div>
          ))}
        </dl>

        <div className={`overview-confirmation-impact ${model.impactTone}`}>
          <span>{model.impactTitle}</span>
          <p>{model.impact}</p>
        </div>
        <p className="overview-confirmation-safety">
          <span aria-hidden="true">✓</span>
          {model.safetyNote}
        </p>

        <footer className="overview-confirmation-actions">
          <button type="button" className="button secondary" disabled={busy} onClick={onCancel}>
            取消
          </button>
          <button
            type="button"
            className="button primary"
            disabled={busy}
            autoFocus
            onClick={props.onConfirm}
          >
            {busy ? "正在提交…" : model.confirmLabel}
          </button>
        </footer>
      </section>
    </div>
  );
}

function AgentActionConfirmationDialog(props: {
  model: AgentActionConfirmation | null;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { model, busy = false, onCancel } = props;

  useBodyScrollLock(model !== null);

  useEffect(() => {
    if (!model) return undefined;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [busy, model, onCancel]);

  if (!model) return null;
  return (
    <div className="overview-confirmation-layer">
      <button
        type="button"
        className="overview-confirmation-backdrop"
        aria-label="取消当前操作"
        disabled={busy}
        onClick={onCancel}
      />
      <section
        className="overview-confirmation agent-action-confirmation"
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-action-confirmation-title"
        aria-describedby="agent-action-confirmation-description"
      >
        <header className="overview-confirmation-head">
          <div className={`overview-confirmation-icon ${model.dangerous ? "danger" : "latest"}`} aria-hidden="true">!</div>
          <div>
            <span className="eyebrow">{model.eyebrow ?? "AGENT CONFIGURATION"}</span>
            <h2 id="agent-action-confirmation-title">{model.title}</h2>
            <p id="agent-action-confirmation-description">{model.description}</p>
          </div>
          <button
            type="button"
            className="overview-confirmation-close"
            aria-label="关闭确认弹窗"
            disabled={busy}
            onClick={onCancel}
          >×</button>
        </header>
        <dl className="overview-confirmation-details">
          {model.details.map((detail) => (
            <div key={detail.label}>
              <dt>{detail.label}</dt>
              <dd className={detail.mono ? "mono" : ""}>{detail.value}</dd>
            </div>
          ))}
        </dl>
        <div className={`overview-confirmation-impact ${model.dangerous ? "attention" : "quiet"}`}>
          <span>{model.impactTitle}</span>
          <p>{model.impact}</p>
        </div>
        <footer className="overview-confirmation-actions">
          <button type="button" className="button secondary" disabled={busy} onClick={onCancel}>取消</button>
          <button
            type="button"
            className={`button ${model.dangerous ? "danger" : "primary"}`}
            disabled={busy}
            autoFocus
            onClick={props.onConfirm}
          >
            {busy ? "正在处理…" : model.confirmLabel}
          </button>
        </footer>
      </section>
    </div>
  );
}

function Overview(props: {
  status: RuntimeStatus;
  events: EventRecord[];
  changeRequests: ChangeRequestRecord[];
  repositories: Repository[];
  changeRequestFilter: OverviewFilter;
  eventFilter: OverviewFilter;
  changeRequestPage: OverviewPage;
  eventPage: OverviewPage;
  emittingKey: string;
  triggeringKeys: string[];
  selectionMode: boolean;
  selectedSnapshotKeys: string[];
  onAction: (action: "scan" | "pause" | "resume") => void;
  onEmitDiscovered: (item: ChangeRequestRecord) => void;
  onTriggerLatestEvent: (item: ChangeRequestRecord) => void;
  onBeginSelection: () => void;
  onCancelSelection: () => void;
  onToggleSelection: (snapshotKey: string) => void;
  onTriggerSelected: (items: ChangeRequestRecord[]) => void;
  onChangeRequestFilterChange: (filter: OverviewFilter) => void;
  onEventFilterChange: (filter: OverviewFilter) => void;
  onChangeRequestPageChange: (page: number) => void;
  onEventPageChange: (page: number) => void;
}) {
  const [selectedChangeRequest, setSelectedChangeRequest] = useState<ChangeRequestRecord | null>(null);
  const [selectedEvent, setSelectedEvent] = useState<EventRecord | null>(null);
  const [selectedExecution, setSelectedExecution] = useState<{
    kind: "agent" | "preflight";
    id: string;
  } | null>(null);
  const runTotal = Object.values(props.status.stats.runs).reduce((sum, value) => sum + value, 0);
  const eventTotal = Object.values(props.status.stats.events).reduce((sum, value) => sum + value, 0);
  const changeRequestTotal = props.status.stats.change_requests.total ?? 0;
  const pendingEvents = (props.status.stats.events.pending ?? 0) + (props.status.stats.events.processing ?? 0);
  const selectedItems = props.changeRequests.filter((item) => (
    item.latest_event && props.selectedSnapshotKeys.includes(item.snapshot_key)
  ));
  return (
    <div className="page-stack">
      <section className="hero-card">
        <div>
          <span className="eyebrow">后台调度器</span>
          <h1>{props.status.paused ? "扫描已暂停" : props.status.running_cycle ? "正在扫描" : props.status.dispatching_events ? "Agent 调度进行中" : "服务运行正常"}</h1>
          <p>配置版本 {shortRevision(props.status.config_revision)} · 最近扫描完成 {timeText(props.status.last_finished_at)}</p>
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
      {(props.status.config_error || props.status.last_error || props.status.last_dispatch_error) && (
        <div className="alert error">{props.status.config_error ?? props.status.last_error ?? props.status.last_dispatch_error}</div>
      )}
      <div className="metric-grid">
        <div className="metric-card"><span>已扫描 MR / PR</span><strong>{changeRequestTotal}</strong><small>{props.status.stats.change_requests.opened ?? 0} 个处于打开状态</small></div>
        <div className="metric-card"><span>变化事件</span><strong>{eventTotal}</strong><small>{pendingEvents} 个待处理</small></div>
        <div className="metric-card"><span>Agent 运行</span><strong>{runTotal}</strong><small>{props.status.stats.runs.running ?? 0} 个执行中 · {props.status.stats.runs.preparing ?? 0} 个准备中 · {props.status.stats.runs.queued ?? 0} 个排队中</small></div>
        <div className="metric-card"><span>最近扫描</span><strong>{props.status.running_cycle ? "进行中" : "已结束"}</strong><small>{timeText(props.status.last_started_at)}</small></div>
      </div>
      <section className="section-card">
        <div className="section-title-row">
          <div><h2>已扫描 MR / PR</h2><p>扫描器在 SQLite 中保存的最新快照；这里的数量与变化事件分开统计。</p></div>
          <div className="overview-section-tools">
            <div className="overview-selection-actions">
              {props.selectionMode ? (
                <>
                  <button className="button secondary compact" onClick={props.onCancelSelection}>取消</button>
                  <button
                    className="button primary compact"
                    disabled={selectedItems.length === 0 || props.triggeringKeys.length > 0}
                    onClick={() => props.onTriggerSelected(selectedItems)}
                  >
                    {props.triggeringKeys.length > 0
                      ? "触发中…"
                      : `手动触发（${selectedItems.length}）`}
                  </button>
                </>
              ) : (
                <button
                  className="button secondary compact"
                  disabled={!props.changeRequests.some((item) => item.latest_event)}
                  title={props.changeRequests.some((item) => item.latest_event)
                    ? "选择多个 MR / PR 批量手动触发"
                    : "当前列表没有可手动触发的最新事件"}
                  onClick={props.onBeginSelection}
                >
                  选择
                </button>
              )}
            </div>
            <OverviewListControls
              repositories={props.repositories}
              filter={props.changeRequestFilter}
              statuses={CHANGE_REQUEST_STATUS_OPTIONS}
              multiStatus
              onChange={props.onChangeRequestFilterChange}
            />
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {props.selectionMode && <th className="overview-selection-column">选择</th>}
                <th>MR / PR</th><th>仓库</th><th>状态</th><th>远端更新</th><th>最近扫描</th><th>首次事件</th><th>最新事件</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              {props.changeRequests.map((item) => (
                <tr
                  key={item.snapshot_key}
                  className={`overview-detail-row ${props.selectedSnapshotKeys.includes(item.snapshot_key) ? "overview-row-selected" : ""}`}
                  tabIndex={0}
                  onClick={() => setSelectedChangeRequest(item)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      setSelectedChangeRequest(item);
                    }
                  }}
                >
                  {props.selectionMode && (
                    <td className="overview-selection-column">
                      <input
                        type="checkbox"
                        aria-label={`选择 ${item.repository_id} #${item.number}`}
                        checked={props.selectedSnapshotKeys.includes(item.snapshot_key)}
                        disabled={!item.latest_event || props.triggeringKeys.length > 0}
                        title={item.latest_event ? "加入批量手动触发" : "尚无可触发的最新事件"}
                        onClick={(event) => event.stopPropagation()}
                        onChange={() => props.onToggleSelection(item.snapshot_key)}
                      />
                    </td>
                  )}
                  <td>
                    <a className="change-request-link" href={item.web_url} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>
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
                        onClick={(event) => {
                          event.stopPropagation();
                          props.onEmitDiscovered(item);
                        }}
                      >
                        {props.emittingKey === item.snapshot_key ? "补发中…" : "补发首次事件"}
                      </button>
                    )}
                  </td>
                  <td>
                    {item.latest_event ? (
                      <span className="latest-event-reference">
                        <strong>{item.latest_event.event_type}</strong>
                        <small>{dateTimeText(item.latest_event.occurred_at)}</small>
                      </span>
                    ) : (
                      <span className="latest-event-empty">
                        {item.latest_event_supported === false
                          ? "当前 Provider 不支持"
                          : item.latest_event_checked
                            ? "暂无可识别事件"
                            : "等待扫描获取"}
                      </span>
                    )}
                  </td>
                  <td>
                    <button
                      className="button secondary compact"
                      disabled={!item.latest_event || props.triggeringKeys.includes(item.snapshot_key)}
                      title={item.latest_event ? `手动发送 ${item.latest_event.event_type}` : "尚无可触发的最新事件"}
                      onClick={(event) => {
                        event.stopPropagation();
                        props.onTriggerLatestEvent(item);
                      }}
                    >
                      {props.triggeringKeys.includes(item.snapshot_key) ? "发送中…" : "手动触发"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {props.changeRequests.length === 0 && (
            <div className="empty">
              {props.changeRequestFilter.repositoryId
                || props.changeRequestFilter.status
                || props.changeRequestFilter.statuses.length > 0
                ? "当前筛选条件下没有 MR / PR。"
                : "尚未扫描到 MR / PR，请确认仓库已启用并执行扫描。"}
            </div>
          )}
        </div>
        <OverviewPagination
          page={props.changeRequestPage}
          onPageChange={props.onChangeRequestPageChange}
        />
      </section>
      <section className="section-card">
        <div className="section-title-row">
          <div><h2>最近变化事件</h2><p>新发现、提交、状态、标签等变化产生的语义事件，不代表 PR 总数。</p></div>
          <OverviewListControls
            repositories={props.repositories}
            filter={props.eventFilter}
            statuses={EVENT_STATUS_OPTIONS}
            multiStatus
            showNumber
            onChange={props.onEventFilterChange}
          />
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>事件</th><th>仓库</th><th>编号</th><th>事件状态</th><th>Agent</th><th>时间</th></tr></thead>
            <tbody>
              {props.events.map((event) => (
                <tr
                  key={event.event_id}
                  className="overview-detail-row"
                  tabIndex={0}
                  onClick={() => setSelectedEvent(event)}
                  onKeyDown={(keyboardEvent) => {
                    if (keyboardEvent.key === "Enter" || keyboardEvent.key === " ") {
                      keyboardEvent.preventDefault();
                      setSelectedEvent(event);
                    }
                  }}
                >
                  <td className="mono">
                    <span className="event-type-with-origin">
                      {event.event_type}
                      {event.origin === "manual" && <small>手动</small>}
                    </span>
                  </td>
                  <td>{event.repository_id}</td>
                  <td>#{event.number}</td>
                  <td>
                    <EventStatusPill event={event} />
                  </td>
                  <td><EventAgentProgress event={event} /></td>
                  <td>{dateTimeText(event.occurred_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {props.events.length === 0 && (
            <div className="empty">
              {props.eventFilter.repositoryId
                || props.eventFilter.number
                || props.eventFilter.status
                || props.eventFilter.statuses.length > 0
                ? "当前筛选条件下没有变化事件。"
                : "尚未产生事件"}
            </div>
          )}
        </div>
        <OverviewPagination
          page={props.eventPage}
          onPageChange={props.onEventPageChange}
        />
      </section>
      <ChangeRequestDetailDrawer
        changeRequest={selectedChangeRequest}
        active={selectedEvent === null && selectedExecution === null}
        depth={0}
        onOpenEvent={setSelectedEvent}
        onClose={() => setSelectedChangeRequest(null)}
      />
      <EventDetailDrawer
        event={selectedEvent}
        active={selectedExecution === null}
        depth={selectedChangeRequest ? 1 : 0}
        onOpenAgent={(runId) => setSelectedExecution({ kind: "agent", id: runId })}
        onOpenPreflight={(runId) => setSelectedExecution({ kind: "preflight", id: runId })}
        onClose={() => setSelectedEvent(null)}
      />
      {selectedExecution?.kind === "agent" && (
        <AgentRunDetailDrawer
          initialRunId={selectedExecution.id}
          depth={(selectedChangeRequest ? 1 : 0) + (selectedEvent ? 1 : 0)}
          onClose={() => setSelectedExecution(null)}
          onRefresh={() => undefined}
        />
      )}
      {selectedExecution?.kind === "preflight" && (
        <PreflightRunDetailDrawer
          runId={selectedExecution.id}
          depth={(selectedChangeRequest ? 1 : 0) + (selectedEvent ? 1 : 0)}
          onClose={() => setSelectedExecution(null)}
        />
      )}
    </div>
  );
}

function GlobalEnvironment(props: {
  document: ConfigDocument;
  codexOptions: CodexRuntimeOptions;
  onChange: (document: ConfigDocument) => void;
}) {
  const protectedNames = providerCredentialNames(props.document);
  const defaultSelection = props.document.runtime.default_model ?? { provider: "codex-cli" };
  const defaultProvider = props.document.model_providers[defaultSelection.provider]
    ?? props.document.model_providers["codex-cli"];
  const providerModels = defaultProvider?.driver === "codex_cli"
    ? props.codexOptions.models
    : (defaultProvider?.models ?? []).map((model) => ({
        slug: model,
        display_name: model,
        supported_reasoning_levels: [],
        supports_fast_mode: false,
      }));
  const resolvedDefaultModel = defaultSelection.model
    || defaultProvider?.default_model
    || (defaultProvider?.driver === "codex_cli"
      ? props.codexOptions.inherited_model.value
      : undefined);
  const defaultModelPlaceholder = defaultProvider?.driver === "codex_cli"
    ? `${props.document.runtime.codex?.execution_mode === "cli" ? "CLI" : "基座"}默认模型（${resolvedDefaultModel ?? "暂未解析"}）`
    : `Provider 默认模型（${resolvedDefaultModel ?? "暂未解析"}）`;
  function patchSection(section: "scanner" | "runtime" | "web", key: string, value: unknown) {
    props.onChange({
      ...props.document,
      [section]: { ...props.document[section], [key]: value },
    });
  }
  return (
    <div className="page-stack">
      <section className="section-card">
        <div className="section-title-row">
          <div>
            <h2>全局默认模型</h2>
            <p>所有没有显式选择模型的 Agent 都继承这里的 Provider 与具体模型。</p>
          </div>
        </div>
        <div className="form-grid three">
          <SelectField
            label="默认 Provider"
            value={defaultSelection.provider}
            onChange={(provider) => patchSection("runtime", "default_model", { provider })}
            options={Object.entries(props.document.model_providers).map(([id, provider]) => ({
              value: id,
              label: `${provider.display_name}${provider.enabled === false ? "（已停用）" : ""}`,
            }))}
            help="系统初始使用不可删除的 Codex CLI Provider"
          />
          <ModelField
            id="global-default-model"
            label="默认模型"
            value={defaultSelection.model ?? ""}
            placeholder={defaultModelPlaceholder}
            models={providerModels}
            onChange={(model) => patchSection("runtime", "default_model", {
              provider: defaultSelection.provider,
              model: model || undefined,
            })}
            help={`当前有效值：${defaultProvider?.display_name ?? defaultSelection.provider} / ${resolvedDefaultModel ?? "暂未解析"}`}
          />
        </div>
      </section>
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
          <Field label="全局并发上限" type="number" value={Number(props.document.runtime.max_concurrent_agents ?? 5)} onChange={(value) => patchSection("runtime", "max_concurrent_agents", Number(value))} help="默认 5；与运行时 Agent 并发数取较小值" />
          <Field label="Agent 运行并发数" type="number" value={Number(props.document.runtime.agent_concurrency_limit ?? 5)} onChange={(value) => patchSection("runtime", "agent_concurrency_limit", Number(value))} help="与全局并发上限取较小值" />
          <Field label="基础仓库初始化超时（秒）" type="number" value={Number(props.document.runtime.repository_initialization_timeout_seconds ?? 1800)} onChange={(value) => patchSection("runtime", "repository_initialization_timeout_seconds", Number(value))} />
          <Field label="Git 操作超时（秒）" type="number" value={Number(props.document.runtime.git_timeout_seconds ?? 600)} onChange={(value) => patchSection("runtime", "git_timeout_seconds", Number(value))} />
          <Field label="默认无进展超时（秒）" type="number" value={Number(props.document.runtime.agent_idle_timeout_seconds ?? 300)} onChange={(value) => patchSection("runtime", "agent_idle_timeout_seconds", Number(value))} />
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
      <section className="section-card">
        <div className="section-title-row"><div><h2>Teamwork 外层沙盒</h2><p>供模型基座和受限 Agent 统一使用的跨平台执行边界。</p></div></div>
        <div className="toggle-grid">
          <Toggle
            label="启用 Teamwork 托管外层沙盒"
            checked={props.document.runtime.managed_sandbox?.enabled ?? true}
            onChange={(enabled) => patchSection("runtime", "managed_sandbox", {
              ...(props.document.runtime.managed_sandbox ?? {}),
              enabled,
            })}
          />
          <Toggle
            label="能力不可用时失败关闭"
            checked={props.document.runtime.managed_sandbox?.fail_closed ?? true}
            disabled={!(props.document.runtime.managed_sandbox?.enabled ?? true)}
            onChange={(fail_closed) => patchSection("runtime", "managed_sandbox", {
              ...(props.document.runtime.managed_sandbox ?? {}),
              fail_closed,
            })}
          />
        </div>
      </section>
    </div>
  );
}

const MODEL_PROVIDER_DRIVER_OPTIONS: Array<{ value: ModelProviderDriver; label: string }> = [
  { value: "openai_responses", label: "OpenAI Responses" },
  { value: "openai_chat_completions", label: "OpenAI Chat Completions" },
  { value: "anthropic_messages", label: "Anthropic Messages" },
  { value: "gemini_generate_content", label: "Gemini GenerateContent" },
];

function modelProviderDriverLabel(driver: ModelProviderDriver): string {
  if (driver === "codex_cli") return "Codex CLI";
  return MODEL_PROVIDER_DRIVER_OPTIONS.find((item) => item.value === driver)?.label ?? driver;
}

function ModelProvidersEditor(props: {
  document: ConfigDocument;
  revision: string;
  codexOptions: CodexRuntimeOptions;
  onSaved: (document: ConfigDocument, revision: string) => void;
  onError: (message: string) => void;
  onNotice: (message: string) => void;
}) {
  const providerIds = useMemo(
    () => Object.keys(props.document.model_providers).sort((left, right) => {
      if (left === "codex-cli") return -1;
      if (right === "codex-cli") return 1;
      return left.localeCompare(right);
    }),
    [props.document.model_providers],
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draftId, setDraftId] = useState("");
  const [draftDocument, setDraftDocument] = useState<ConfigDocument | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [snapshot, setSnapshot] = useState<ModelProviderSnapshot | null>(null);
  const [visibleKey, setVisibleKey] = useState("");
  const [keyInput, setKeyInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [connectionResult, setConnectionResult] = useState("");

  useBodyScrollLock(selectedId !== null);

  const reloadSnapshot = useCallback(async () => {
    try {
      setSnapshot(await api<ModelProviderSnapshot>("/api/model-providers"));
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "Provider 状态加载失败");
    }
  }, [props.onError]);

  useEffect(() => {
    void reloadSnapshot();
  }, [props.revision, reloadSnapshot]);

  useEffect(() => {
    setVisibleKey("");
  }, [props.revision]);

  const activeDocument = editing && draftDocument ? draftDocument : props.document;
  const activeId = editing ? draftId : selectedId ?? "";
  const selected = activeDocument.model_providers[activeId];
  const original = selectedId ? props.document.model_providers[selectedId] : undefined;
  const selectedSnapshot = snapshot?.providers.find((item) => item.id === selectedId);
  const external = selected?.driver !== "codex_cli";

  function codexRuntimePayload(document: ConfigDocument) {
    return {
      codex_binary: document.runtime.codex_binary ?? "codex",
      codex_home: document.runtime.codex_home ?? null,
      expected_codex_version: document.runtime.expected_codex_version ?? null,
      inherit_user_mcp_servers: document.runtime.inherit_user_mcp_servers ?? false,
      allowed_user_mcp_servers: document.runtime.allowed_user_mcp_servers ?? [],
      codex: document.runtime.codex ?? {
        execution_mode: "model",
        fast_mode: "inherit",
        extra_config: {},
      },
    };
  }

  const dirty = editing && Boolean(
    creating
    || draftId !== selectedId
    || JSON.stringify(selected) !== JSON.stringify(original)
    || (
      selected?.driver === "codex_cli"
      && JSON.stringify(codexRuntimePayload(activeDocument))
        !== JSON.stringify(codexRuntimePayload(props.document))
    ),
  );
  const idConflict = creating
    && draftId.trim() !== ""
    && Object.hasOwn(props.document.model_providers, draftId.trim());

  function clearDraft() {
    setCreating(false);
    setEditing(false);
    setDraftDocument(null);
    setDraftId("");
    setKeyInput("");
  }

  function confirmDiscard(): boolean {
    return !dirty || window.confirm("当前 Provider 有未保存修改，确认放弃这些修改？");
  }

  function openDetail(providerId: string) {
    if (!confirmDiscard()) return;
    clearDraft();
    setSelectedId(providerId);
    setVisibleKey("");
    setConnectionResult("");
  }

  function closeDetail() {
    if (!confirmDiscard()) return;
    clearDraft();
    setSelectedId(null);
    setVisibleKey("");
    setConnectionResult("");
  }

  useEffect(() => {
    if (selectedId === null) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) closeDetail();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [selectedId, busy, dirty]);

  function beginCreate() {
    if (!confirmDiscard()) return;
    let index = 1;
    while (props.document.model_providers[`provider-${index}`]) index += 1;
    const providerId = `provider-${index}`;
    const nextDocument = structuredClone(props.document);
    nextDocument.model_providers[providerId] = {
      display_name: `API Provider ${index}`,
      driver: "openai_responses",
      enabled: true,
      base_url: "https://api.openai.com",
      request_timeout_seconds: 120,
      models: [],
    };
    setSelectedId(providerId);
    setDraftId(providerId);
    setDraftDocument(nextDocument);
    setCreating(true);
    setEditing(true);
    setVisibleKey("");
    setKeyInput("");
    setConnectionResult("");
  }

  function beginEdit() {
    if (!selectedId || !props.document.model_providers[selectedId]) return;
    setDraftId(selectedId);
    setDraftDocument(structuredClone(props.document));
    setCreating(false);
    setEditing(true);
    setVisibleKey("");
    setConnectionResult("");
  }

  function cancelEdit() {
    if (creating) {
      clearDraft();
      setSelectedId(null);
      return;
    }
    clearDraft();
  }

  function updateProvider(patch: Partial<ModelProviderConfig>) {
    if (!draftDocument || !selected) return;
    setDraftDocument({
      ...draftDocument,
      model_providers: {
        ...draftDocument.model_providers,
        [draftId]: { ...selected, ...patch },
      },
    });
  }

  function updateDraftProviderId(nextId: string) {
    if (!creating || !draftDocument || !selected) return;
    const nextProviders = { ...draftDocument.model_providers };
    delete nextProviders[draftId];
    nextProviders[nextId] = selected;
    setDraftId(nextId);
    setSelectedId(nextId);
    setDraftDocument({ ...draftDocument, model_providers: nextProviders });
  }

  async function saveProvider() {
    if (!editing || !draftDocument || !selected || !draftId.trim()) return;
    setBusy(true);
    props.onError("");
    try {
      const endpoint = creating
        ? "/api/model-providers"
        : `/api/model-providers/${encodeURIComponent(selectedId ?? "")}`;
      const result = await api<{
        revision: string;
        document: ConfigDocument;
        model_providers: ModelProviderSnapshot;
      }>(endpoint, {
        method: creating ? "POST" : "PUT",
        body: JSON.stringify({
          revision: props.revision,
          provider_id: draftId.trim(),
          provider: selected,
          codex_runtime: selected.driver === "codex_cli"
            ? codexRuntimePayload(draftDocument)
            : undefined,
          api_key: external && keyInput.trim() ? keyInput : undefined,
        }),
      });
      const normalized = normalizeDocument(result.document);
      props.onSaved(normalized, result.revision);
      setSnapshot(result.model_providers);
      setSelectedId(draftId.trim());
      clearDraft();
      props.onNotice(creating
        ? `Provider ${draftId.trim()} 已创建并热加载`
        : `Provider ${draftId.trim()} 已保存并热加载`);
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "Provider 保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function revealKey() {
    if (!selectedId) return;
    if (visibleKey) {
      setVisibleKey("");
      return;
    }
    try {
      const result = await api<{ api_key: string }>(
        `/api/model-providers/${encodeURIComponent(selectedId)}/key`,
      );
      setVisibleKey(result.api_key);
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "API Key 查看失败");
    }
  }

  async function replaceKey() {
    if (!selectedId || !keyInput.trim()) return;
    setBusy(true);
    try {
      const result = await api<ModelProviderSnapshot>(
        `/api/model-providers/${encodeURIComponent(selectedId)}/key`,
        {
          method: "PUT",
          body: JSON.stringify({ api_key: keyInput }),
        },
      );
      setSnapshot(result);
      setKeyInput("");
      setVisibleKey("");
      props.onNotice("API Key 已安全保存");
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "API Key 保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function refreshModels() {
    if (!selectedId) return;
    setBusy(true);
    try {
      const result = await api<{
        revision: string;
        document: ConfigDocument;
        model_providers: ModelProviderSnapshot;
      }>(`/api/model-providers/${encodeURIComponent(selectedId)}/refresh-models`, {
        method: "POST",
        body: JSON.stringify({ revision: props.revision }),
      });
      props.onSaved(normalizeDocument(result.document), result.revision);
      setSnapshot(result.model_providers);
      props.onNotice("模型目录已刷新");
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "模型目录刷新失败");
    } finally {
      setBusy(false);
    }
  }

  async function testConnection() {
    if (!selectedId) return;
    setBusy(true);
    setConnectionResult("");
    try {
      const result = await api<CodexConnectionTestResult & { provider_id: string }>(
        `/api/model-providers/${encodeURIComponent(selectedId)}/connection-test`,
        { method: "POST" },
      );
      setConnectionResult(
        `${result.model ?? "默认模型"} · ${result.elapsed_seconds.toFixed(2)} 秒 · ${result.reply}`,
      );
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "Provider 连接测试失败");
    } finally {
      setBusy(false);
    }
  }

  async function deleteProvider() {
    if (!selectedId || !selectedSnapshot?.deletable || !selected) return;
    const impacts = [
      selectedSnapshot.is_global_default ? "全局默认模型将回退到 Codex CLI" : "",
      selectedSnapshot.referenced_agents.length
        ? `${selectedSnapshot.referenced_agents.length} 个 Agent 将改为继承全局默认模型`
        : "",
    ].filter(Boolean).join("；");
    if (!window.confirm(`删除 ${selected.display_name}${impacts ? `；${impacts}` : ""}？`)) return;
    setBusy(true);
    try {
      const result = await api<{
        revision: string;
        document: ConfigDocument;
        model_providers: ModelProviderSnapshot;
      }>(`/api/model-providers/${encodeURIComponent(selectedId)}`, {
        method: "DELETE",
        body: JSON.stringify({ revision: props.revision }),
      });
      props.onSaved(normalizeDocument(result.document), result.revision);
      setSnapshot(result.model_providers);
      clearDraft();
      setSelectedId(null);
      setVisibleKey("");
      props.onNotice("Provider 已删除，相关引用已回退");
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "Provider 删除失败");
    } finally {
      setBusy(false);
    }
  }

  const selectedResolved = selected
    ? resolvedProviderModel(activeDocument, props.codexOptions, activeId)
    : null;
  const modelOptions = selected
    ? (selected.models ?? []).map((model) => ({
        slug: model,
        display_name: model,
        supported_reasoning_levels: [],
        supports_fast_mode: false,
      }))
    : [];

  return (
    <>
      <section className="section-card provider-list-page">
        <div className="section-title-row provider-list-title">
          <div>
            <h2>Provider</h2>
            <p>点击一行查看详情；每个 Provider 独立编辑、保存和测试连接。</p>
          </div>
          <button className="button primary" type="button" onClick={beginCreate}>+ 新增 API Provider</button>
        </div>
        <div className="provider-config-table">
          <div className="provider-config-table-head" aria-hidden="true">
            <span>Provider</span><span>协议 / 模式</span><span>默认模型</span><span>状态</span><span />
          </div>
          <div className="provider-config-items">
            {providerIds.map((providerId) => {
              const provider = props.document.model_providers[providerId];
              const item = snapshot?.providers.find((candidate) => candidate.id === providerId);
              const resolved = resolvedProviderModel(
                props.document,
                props.codexOptions,
                providerId,
              );
              const mode = provider.driver === "codex_cli"
                ? props.document.runtime.codex?.execution_mode === "cli"
                  ? "完整 CLI 模式"
                  : "基座模式"
                : modelProviderDriverLabel(provider.driver);
              return (
                <button
                  key={providerId}
                  type="button"
                  className="provider-config-row"
                  onClick={() => openDetail(providerId)}
                >
                  <span className="provider-config-identity">
                    <span className="provider-config-avatar" aria-hidden="true">P</span>
                    <span>
                      <strong>{provider.display_name}</strong>
                      <small>{providerId}</small>
                    </span>
                  </span>
                  <span><strong>{provider.driver === "codex_cli" ? "Codex CLI" : modelProviderDriverLabel(provider.driver)}</strong><small>{mode}</small></span>
                  <span><strong>{resolved.concrete}</strong><small>{provider.default_model ? "Provider 默认" : "动态解析"}</small></span>
                  <span className="provider-config-badges">
                    <em className={provider.enabled === false ? "disabled" : "enabled"}>
                      {provider.enabled === false ? "已停用" : "已启用"}
                    </em>
                    {item?.is_global_default && <em>全局默认</em>}
                    {item?.builtin && <em>内置</em>}
                  </span>
                  <span className="provider-config-arrow" aria-hidden="true">›</span>
                </button>
              );
            })}
          </div>
        </div>
      </section>

      {selectedId !== null && selected && (
        <div className="run-drawer-layer provider-detail-layer">
          <button
            type="button"
            className="run-drawer-backdrop"
            aria-label="关闭 Provider 详情"
            onClick={closeDetail}
          />
          <aside
            className="run-drawer provider-detail-drawer"
            role="dialog"
            aria-modal="true"
            aria-label={`${selected.display_name} Provider 详情`}
          >
            <header className="run-drawer-head provider-detail-head">
              <div>
                <span className="eyebrow">{activeId}</span>
                <h2>{selected.display_name}</h2>
                <p>{creating ? "尚未保存的新 Provider" : editing ? "当前修改仅保存在详情草稿" : "当前已保存配置"}</p>
              </div>
              <div className="run-drawer-actions provider-detail-actions">
                {editing ? (
                  <>
                    <button className="button secondary" type="button" disabled={busy} onClick={cancelEdit}>取消</button>
                    <button className="button primary" type="button" disabled={busy || !dirty || !draftId.trim() || idConflict} onClick={() => void saveProvider()}>
                      {busy ? "保存中…" : "保存 Provider"}
                    </button>
                  </>
                ) : (
                  <>
                    <button className="button secondary" type="button" disabled={busy || !selectedSnapshot} onClick={() => void testConnection()}>
                      {busy ? "测试中…" : "连接测试"}
                    </button>
                    {selectedSnapshot?.deletable && <button className="button danger" type="button" disabled={busy} onClick={() => void deleteProvider()}>删除</button>}
                    <button className="button primary" type="button" onClick={beginEdit}>编辑 Provider</button>
                  </>
                )}
                <button className="run-drawer-close" type="button" aria-label="关闭" disabled={busy} onClick={closeDetail}>×</button>
              </div>
            </header>
            <div className="run-drawer-body provider-detail-body">
              {connectionResult && <div className="alert success">{connectionResult}</div>}
              <section className="section-card">
                <div className="section-title-row">
                  <div><h2>基本配置</h2><p>Provider 身份稳定；保存只更新当前 Provider。</p></div>
                </div>
                <fieldset className="config-editor-surface" disabled={!editing || busy}>
                  <div className="form-grid three">
                    {creating ? (
                      <Field label="Provider ID" value={draftId} onChange={updateDraftProviderId} help={idConflict ? "该 Provider ID 已存在" : "保存后不可修改"} />
                    ) : (
                      <Field label="Provider ID" value={activeId} disabled onChange={() => undefined} />
                    )}
                    <Field label="显示名称" value={selected.display_name} onChange={(display_name) => updateProvider({ display_name })} />
                    <SelectField
                      label="驱动协议"
                      value={selected.driver}
                      disabled={activeId === "codex-cli"}
                      onChange={(driver) => updateProvider({ driver: driver as ModelProviderDriver })}
                      options={activeId === "codex-cli"
                        ? [{ value: "codex_cli", label: "Codex CLI（内置）" }]
                        : MODEL_PROVIDER_DRIVER_OPTIONS}
                    />
                    <Toggle label="启用 Provider" checked={selected.enabled !== false} onChange={(enabled) => updateProvider({ enabled })} />
                    {external && <Field label="Base URL" value={selected.base_url ?? ""} onChange={(base_url) => updateProvider({ base_url })} placeholder="https://api.example.com" />}
                    <ModelField
                      id={`provider-model-${activeId}`}
                      label="Provider 默认模型"
                      value={selected.default_model ?? ""}
                      placeholder={selectedResolved?.defaultLabel ?? "Provider 默认模型"}
                      models={selected.driver === "codex_cli" ? props.codexOptions.models : modelOptions}
                      onChange={(default_model) => updateProvider({ default_model: default_model || undefined })}
                      help={`当前解析：${selected.display_name} / ${selectedResolved?.concrete ?? "暂未解析"}`}
                    />
                    {external && <Field label="请求超时（秒）" type="number" value={Number(selected.request_timeout_seconds ?? 120)} onChange={(value) => updateProvider({ request_timeout_seconds: Number(value) })} />}
                    {external && <Field label="并发上限（可选）" type="number" value={selected.max_concurrency ?? ""} onChange={(value) => updateProvider({ max_concurrency: value ? Number(value) : null })} />}
                    {external && (
                      <SelectField
                        label="默认推理强度（可选）"
                        value={selected.model_reasoning_effort ?? ""}
                        onChange={(model_reasoning_effort) => updateProvider({ model_reasoning_effort: model_reasoning_effort || undefined })}
                        options={[
                          { value: "", label: "不向上游显式传递" },
                          ...REASONING_LEVELS.map((value) => ({ value, label: value })),
                        ]}
                      />
                    )}
                    {external && (
                      <SelectField
                        label="默认输出详细度（可选）"
                        value={selected.model_verbosity ?? ""}
                        onChange={(model_verbosity) => updateProvider({ model_verbosity: model_verbosity ? model_verbosity as ModelProviderConfig["model_verbosity"] : undefined })}
                        options={[
                          { value: "", label: "使用 Provider / 模型默认" },
                          { value: "low", label: "低" },
                          { value: "medium", label: "中" },
                          { value: "high", label: "高" },
                        ]}
                      />
                    )}
                    {external && (
                      <SelectField
                        label="默认交互风格（可选）"
                        value={selected.personality ?? ""}
                        onChange={(personality) => updateProvider({ personality: personality ? personality as ModelProviderConfig["personality"] : undefined })}
                        options={[
                          { value: "", label: "无 Provider 预设" },
                          { value: "none", label: "无预设" },
                          { value: "friendly", label: "友好" },
                          { value: "pragmatic", label: "务实" },
                        ]}
                      />
                    )}
                  </div>
                  {external && (
                    <label className="field provider-model-list-field">
                      <span>手工模型目录</span>
                      <textarea
                        rows={5}
                        value={(selected.models ?? []).join("\n")}
                        onChange={(event) => updateProvider({
                          models: event.target.value.split("\n").map((item) => item.trim()).filter(Boolean),
                        })}
                        placeholder="每行一个模型 ID"
                      />
                      <small>保存后可使用“检测并刷新模型”读取上游目录。</small>
                    </label>
                  )}
                </fieldset>
              </section>

              {external && (
                <section className="section-card">
                  <div className="section-title-row">
                    <div><h2>API Key</h2><p>默认脱敏；小眼睛只在当前详情临时显示明文。</p></div>
                    {!creating && (
                      <button className="button secondary" type="button" disabled={!selectedSnapshot?.api_key_configured} onClick={() => void revealKey()}>
                        {visibleKey ? "隐藏明文" : "查看明文"}
                      </button>
                    )}
                  </div>
                  {!creating && <div className="credential-value mono">{visibleKey || selectedSnapshot?.masked_key || "尚未配置"}</div>}
                  <div className="form-grid two">
                    <Field
                      label={selectedSnapshot?.api_key_configured ? "替换 API Key" : "API Key"}
                      type="password"
                      value={keyInput}
                      onChange={setKeyInput}
                      help={creating ? "将与新 Provider 一起安全保存" : undefined}
                    />
                    <div className="field-action">
                      {!creating && <button className="button secondary" type="button" disabled={busy || !keyInput.trim()} onClick={() => void replaceKey()}>安全保存</button>}
                      {!creating && <button className="button secondary" type="button" disabled={busy || editing || !selectedSnapshot?.api_key_configured} onClick={() => void refreshModels()}>检测并刷新模型</button>}
                    </div>
                  </div>
                </section>
              )}

              {selected.driver === "codex_cli" && (
                <>
                  <CodexRuntimeEditor
                    document={activeDocument}
                    options={props.codexOptions}
                    editable={editing && !busy}
                    onChange={setDraftDocument}
                  />
                  <CodexAccountCard
                    configuredHome={props.document.runtime.codex_home ? String(props.document.runtime.codex_home) : undefined}
                    homeHasUnsavedChange={String(activeDocument.runtime.codex_home ?? "") !== String(props.document.runtime.codex_home ?? "")}
                  />
                </>
              )}
            </div>
          </aside>
        </div>
      )}
    </>
  );
}

function effectiveInheritedModel(
  document: ConfigDocument,
  options: CodexRuntimeOptions,
): { value?: string | null; label: string } {
  const providerModel = document.model_providers["codex-cli"]?.default_model?.trim();
  if (providerModel) {
    return {
      value: providerModel,
      label: `${document.runtime.codex?.execution_mode === "cli" ? "CLI" : "基座"}默认模型（${providerModel}）`,
    };
  }
  if (options.codex_model) {
    return {
      value: options.codex_model,
      label: options.codex_model_source === "codex"
        ? `继承 Codex 有效配置（${options.codex_model}）`
        : `继承 Codex 用户配置（${options.codex_model}）`,
    };
  }
  return {
    value: null,
    label: `${document.runtime.codex?.execution_mode === "cli" ? "CLI" : "基座"}默认模型（暂未解析）`,
  };
}

function resolvedProviderModel(
  document: ConfigDocument,
  options: CodexRuntimeOptions,
  providerId: string,
  configuredModel?: string | null,
): {
  providerName: string;
  model?: string;
  concrete: string;
  defaultLabel: string;
  models: CodexRuntimeOptions["models"];
} {
  const provider = document.model_providers[providerId];
  const codexProvider = provider?.driver === "codex_cli";
  const model = configuredModel
    || provider?.default_model
    || (codexProvider ? options.inherited_model.value ?? undefined : undefined);
  const concrete = model ?? (codexProvider
    ? "暂未解析：Codex 配置与账号未返回具体模型"
    : "暂未解析：Provider 未配置默认模型");
  const models = codexProvider
    ? options.models
    : (provider?.models ?? []).map((item) => ({
        slug: item,
        display_name: item,
        supported_reasoning_levels: [],
        supports_fast_mode: false,
      }));
  return {
    providerName: provider?.display_name ?? providerId,
    model,
    concrete,
    defaultLabel: provider?.driver === "codex_cli"
      ? `${document.runtime.codex?.execution_mode === "cli" ? "CLI" : "基座"}默认模型（${concrete}）`
      : `Provider 默认模型（${concrete}）`,
    models,
  };
}

function agentModelDisplay(
  document: ConfigDocument,
  options: CodexRuntimeOptions,
  agent: Agent,
): string {
  if (agent.model_provider) {
    const resolved = resolvedProviderModel(document, options, agent.model_provider, agent.model);
    return agent.model
      ? `${resolved.providerName} / ${agent.model}`
      : `${resolved.providerName} / ${resolved.defaultLabel}`;
  }
  const globalDefault = document.runtime.default_model ?? { provider: "codex-cli" };
  const resolved = resolvedProviderModel(document, options, globalDefault.provider, globalDefault.model);
  return `继承全局默认（${resolved.providerName} / ${resolved.concrete}）`;
}

type InheritedSettingKey = keyof CodexRuntimeOptions["inherited_settings"];

const SETTING_VALUE_LABELS: Record<InheritedSettingKey, Record<string, string>> = {
  model_reasoning_effort: {
    minimal: "最小",
    low: "低",
    medium: "中",
    high: "高",
    xhigh: "超高",
    max: "最大",
    ultra: "极高",
  },
  fast_mode: {
    standard: "标准模式",
    default: "标准模式",
    fast: "快速模式",
    priority: "快速模式",
  },
  model_verbosity: { low: "低", medium: "中", high: "高" },
  personality: { none: "无预设", friendly: "友好", pragmatic: "务实" },
  web_search: {
    disabled: "禁用",
    cached: "缓存搜索",
    indexed: "索引搜索",
    live: "实时搜索",
  },
};

const SETTING_SOURCE_LABELS: Record<CodexInheritedSetting["source"], string> = {
  codex: "Codex 有效配置",
  user: "Codex 用户配置",
  model: "模型默认",
  builtin: "Codex 默认",
  runtime: "运行时默认",
  provider: "Provider 默认",
  unknown: "Codex / 模型默认",
};

function settingValueLabel(key: InheritedSettingKey, setting: CodexInheritedSetting): string {
  if (!setting.known || !setting.value) return "UNK";
  return SETTING_VALUE_LABELS[key][setting.value] ?? setting.value;
}

function baseInheritedSetting(
  options: CodexRuntimeOptions,
  key: InheritedSettingKey,
  model?: string | null,
  sandbox?: Agent["sandbox"],
): CodexInheritedSetting {
  const configured = options.inherited_settings?.[key] ?? {
    value: null,
    source: "unknown",
    known: false,
  };
  if (key === "model_reasoning_effort" && !configured.known) {
    const modelEntry = options.models.find((item) => item.slug === model);
    if (modelEntry?.default_reasoning_level) {
      return {
        value: modelEntry.default_reasoning_level,
        source: "model",
        known: true,
      };
    }
  }
  if (
    key === "web_search"
    && configured.source === "builtin"
    && sandbox === "danger-full-access"
  ) {
    return { value: "live", source: "builtin", known: true };
  }
  return configured;
}

function runtimeInheritedSetting(
  document: ConfigDocument,
  options: CodexRuntimeOptions,
  key: InheritedSettingKey,
  model?: string | null,
  sandbox?: Agent["sandbox"],
  provider?: ModelProviderConfig,
): CodexInheritedSetting {
  if (provider && provider.driver !== "codex_cli") {
    const providerValues: Partial<Record<InheritedSettingKey, string | undefined>> = {
      model_reasoning_effort: provider.model_reasoning_effort,
      fast_mode: "standard",
      model_verbosity: provider.model_verbosity,
      personality: provider.personality,
      web_search: "disabled",
    };
    const value = providerValues[key];
    return { value: value ?? null, source: "provider", known: Boolean(value) };
  }
  const codex = document.runtime.codex ?? {};
  const explicitValue = codex[key];
  const hasExplicitValue = key === "fast_mode"
    ? Boolean(explicitValue && explicitValue !== "inherit")
    : Boolean(explicitValue);
  if (hasExplicitValue) {
    return {
      value: String(explicitValue),
      source: "runtime",
      known: true,
    };
  }
  return baseInheritedSetting(options, key, model, sandbox);
}

function runtimeInheritanceLabel(
  key: InheritedSettingKey,
  setting: CodexInheritedSetting,
): string {
  return `继承 ${SETTING_SOURCE_LABELS[setting.source]}（${settingValueLabel(key, setting)}）`;
}

function agentInheritanceLabel(
  key: InheritedSettingKey,
  setting: CodexInheritedSetting,
): string {
  const value = settingValueLabel(key, setting);
  if (value === "UNK") return `继承 ${SETTING_SOURCE_LABELS[setting.source]}（UNK）`;
  return `继承运行时默认（${value} · ${SETTING_SOURCE_LABELS[setting.source]}）`;
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
  editable: boolean;
  onChange: (document: ConfigDocument) => void;
}) {
  const codex = props.document.runtime.codex ?? {};
  const inherited = effectiveInheritedModel(props.document, props.options);
  const selectedModel = props.document.model_providers["codex-cli"]?.default_model
    || props.options.codex_model;
  const inheritedReasoning = baseInheritedSetting(
    props.options,
    "model_reasoning_effort",
    selectedModel,
  );
  const inheritedFastMode = baseInheritedSetting(props.options, "fast_mode", selectedModel);
  const inheritedVerbosity = baseInheritedSetting(props.options, "model_verbosity", selectedModel);
  const inheritedPersonality = baseInheritedSetting(props.options, "personality", selectedModel);
  const inheritedWebSearch = baseInheritedSetting(props.options, "web_search", selectedModel);

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
            <h2>Codex CLI 配置</h2>
            <p>配置内置 Codex CLI 的命令、账号环境和驱动专属默认参数。</p>
          </div>
        </div>
        <fieldset className="config-editor-surface" disabled={!props.editable}>
        <div className="runtime-mode-stack">
          <div className="agent-workspace-note">
            <strong>当前模型来源</strong>
            <span>
              {inherited.label}。Agent 未显式选择模型时由“全局配置与环境”的默认模型决定；
              仓库中的 .codex/config.toml 仍可能参与 Codex CLI 原生合并。
            </span>
          </div>
          <div className="agent-workspace-note">
            <strong>实际后台 CLI</strong>
            <span>
              {props.options.binary.resolved_path ?? String(props.document.runtime.codex_binary ?? "codex")}
              {props.options.binary.version ? ` · ${props.options.binary.version}` : " · 版本无法识别"}
              {` · CODEX_HOME ${props.options.codex_home}`}
            </span>
          </div>
        </div>
        {props.options.version_warning && <div className="alert error">{props.options.version_warning}</div>}
        {props.options.effective_config_error && (
          <div className="alert error">
            无法通过 Codex 读取完整有效配置，当前继承值已回退到用户配置；无法确定的字段显示 UNK。
          </div>
        )}
        <div className="form-grid three runtime-config-grid">
          <SelectField
            label="Codex 执行模式"
            value={codex.execution_mode ?? "model"}
            onChange={(value) => patchCodex({ execution_mode: value as CodexRuntimeConfig["execution_mode"] })}
            options={[
              { value: "model", label: "基座模式（默认）" },
              { value: "cli", label: "完整 Codex CLI 模式" },
            ]}
            help="基座模式复用 Codex OAuth 登录态并由 Teamwork 执行工具循环"
          />
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
          <SelectField
            label="默认推理强度"
            value={codex.model_reasoning_effort ?? ""}
            onChange={(value) => patchCodex({ model_reasoning_effort: value || undefined })}
            options={[
              {
                value: "",
                label: runtimeInheritanceLabel("model_reasoning_effort", inheritedReasoning),
              },
              ...reasoningLevels(props.options, selectedModel, codex.model_reasoning_effort).map((value) => ({ value, label: value })),
            ]}
            help="不同模型支持的强度可能不同"
          />
          <SelectField
            label="快速模式"
            value={codex.fast_mode ?? "inherit"}
            onChange={(value) => patchCodex({ fast_mode: value as CodexRuntimeConfig["fast_mode"] })}
            options={[
              {
                value: "inherit",
                label: runtimeInheritanceLabel("fast_mode", inheritedFastMode),
              },
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
              {
                value: "",
                label: runtimeInheritanceLabel("model_verbosity", inheritedVerbosity),
              },
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
              {
                value: "",
                label: runtimeInheritanceLabel("personality", inheritedPersonality),
              },
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
              {
                value: "",
                label: runtimeInheritanceLabel("web_search", inheritedWebSearch),
              },
              { value: "disabled", label: "禁用" },
              { value: "cached", label: "缓存搜索" },
              { value: "live", label: "实时搜索" },
            ]}
          />
        </div>
        </fieldset>
      </section>
      <section className="section-card">
        <div className="section-title-row">
          <div>
            <h2>后台 MCP 能力隔离</h2>
            <p>默认关闭用户 Codex 配置中的 MCP，只保留 Teamwork sub-agent 网关；平台操作优先使用 MR / PR 输入、API、gh / glab 和本地工作区。</p>
          </div>
        </div>
        <fieldset className="config-editor-surface" disabled={!props.editable}>
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
        </fieldset>
      </section>
      <section className="section-card">
        <div className="section-title-row">
          <div>
            <h2>高级 Codex 配置</h2>
            <p>
              使用 Codex 原生点号键。值按 JSON 编写；结构化字段、安全策略、MCP 和 Skill 不能在这里覆盖。
            </p>
          </div>
        </div>
        <fieldset className="config-editor-surface" disabled={!props.editable}>
        <JsonEditor
          label="额外配置（JSON）"
          value={codex.extra_config ?? {}}
          help={'示例：{ "features.some_flag": true, "history.max_bytes": 1048576 }'}
          onChange={(extra_config) => patchCodex({
            extra_config: extra_config as CodexRuntimeConfig["extra_config"],
          })}
        />
        </fieldset>
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

function RepositoryConnectionsEditor(props: {
  document: ConfigDocument;
  revision: string;
  onSaved: (document: ConfigDocument, revision: string) => void;
  onError: (message: string) => void;
  onNotice: (message: string) => void;
}) {
  const [editingName, setEditingName] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [draftProvider, setDraftProvider] = useState<Record<string, unknown> | null>(null);
  const [saving, setSaving] = useState(false);
  const providerNames = Object.keys(props.document.providers);
  const repositoryCount = props.document.repositories.length;

  function providerDefaults(kind: "github" | "gitlab") {
    return kind === "gitlab"
      ? { base_url: "https://gitlab.com/api/v4", token_env: "GITLAB_TOKEN" }
      : { base_url: "https://api.github.com", token_env: "GITHUB_TOKEN" };
  }

  function addProvider() {
    if (editingName !== null) return;
    let index = providerNames.length + 1;
    let name = `provider-${index}`;
    while (props.document.providers[name]) name = `provider-${++index}`;
    setCreating(true);
    setEditingName(name);
    setDraftName(name);
    setDraftProvider({ kind: "github", ...providerDefaults("github") });
  }

  function beginEdit(name: string) {
    if (editingName !== null) return;
    setCreating(false);
    setEditingName(name);
    setDraftName(name);
    setDraftProvider(structuredClone(props.document.providers[name]));
  }

  function clearDraft() {
    setEditingName(null);
    setCreating(false);
    setDraftName("");
    setDraftProvider(null);
  }

  function updateDraft(patch: Record<string, unknown>) {
    setDraftProvider((current) => ({ ...(current ?? {}), ...patch }));
  }

  async function saveProvider() {
    if (!editingName || !draftName.trim() || !draftProvider) return;
    setSaving(true);
    props.onError("");
    try {
      const endpoint = creating
        ? "/api/config/providers"
        : `/api/config/providers/${encodeURIComponent(editingName)}`;
      const result = await api<{ revision: string; document: ConfigDocument }>(endpoint, {
        method: creating ? "POST" : "PUT",
        body: JSON.stringify({
          revision: props.revision,
          name: draftName.trim(),
          provider: draftProvider,
        }),
      });
      props.onSaved(normalizeDocument(result.document), result.revision);
      props.onNotice(
        creating
          ? `平台连接 ${draftName.trim()} 已创建并热加载`
          : `平台连接 ${draftName.trim()} 已保存并热加载`,
      );
      clearDraft();
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "保存平台连接失败");
    } finally {
      setSaving(false);
    }
  }

  async function deleteProvider(name: string) {
    if (saving) return;
    setSaving(true);
    props.onError("");
    try {
      const result = await api<{ revision: string; document: ConfigDocument }>(
        `/api/config/providers/${encodeURIComponent(name)}`,
        {
          method: "DELETE",
          body: JSON.stringify({ revision: props.revision }),
        },
      );
      props.onSaved(normalizeDocument(result.document), result.revision);
      props.onNotice(`平台连接 ${name} 已删除并热加载`);
      clearDraft();
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "删除平台连接失败");
    } finally {
      setSaving(false);
    }
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
          <button className="button secondary" disabled={editingName !== null} onClick={addProvider}>+ 添加平台连接</button>
        </div>
        <div className="card-list compact">
          {providerNames.length === 0 && (
            <div className="empty-config-state">
              <strong>还没有 GitHub / GitLab 连接</strong>
              <p>先点击“添加平台连接”，再配置平台 API 地址，以及宿主机中保存访问 Token 的环境变量名。</p>
            </div>
          )}
          {[...providerNames, ...(creating && editingName ? [editingName] : [])].map((name) => {
            const isEditing = editingName === name;
            const provider = isEditing && draftProvider
              ? draftProvider
              : props.document.providers[name];
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
              <article className={`sub-card provider-card ${isEditing ? "editing" : ""}`} key={name}>
                <div className="sub-card-head provider-card-head">
                  <div>
                    <span className="eyebrow">{creating && isEditing ? "NEW PROVIDER" : "PROVIDER CONNECTION"}</span>
                    <h3>{creating && isEditing ? "新建平台连接" : name}</h3>
                  </div>
                  <div className="button-group">
                    {isEditing ? (
                      <>
                        {!creating && (
                          <button
                            type="button"
                            className="button danger compact"
                            disabled={saving || referencedRepositories > 0}
                            title={referencedRepositories > 0 ? `有 ${referencedRepositories} 个仓库正在使用此连接` : "删除连接"}
                            onClick={() => { void deleteProvider(name); }}
                          >删除连接</button>
                        )}
                        <button type="button" className="button secondary compact" disabled={saving} onClick={clearDraft}>取消</button>
                        <button
                          type="button"
                          className="button primary compact"
                          disabled={saving || !draftName.trim()}
                          onClick={() => { void saveProvider(); }}
                        >{saving ? "保存中…" : "保存连接"}</button>
                      </>
                    ) : (
                      <button
                        type="button"
                        className="button secondary compact"
                        disabled={editingName !== null}
                        onClick={() => beginEdit(name)}
                      >编辑</button>
                    )}
                  </div>
                </div>
                <fieldset className="config-editor-surface provider-editor-surface" disabled={!isEditing || saving}>
                  <div className="provider-row">
                    <Field label="连接名称" value={isEditing ? draftName : name} onChange={setDraftName} />
                    <SelectField
                      label="代码平台"
                      value={String(provider.kind)}
                      onChange={(value) => {
                        const kind = value as "github" | "gitlab";
                        updateDraft({ kind, ...providerDefaults(kind) });
                      }}
                      options={[
                        { value: "github", label: "GitHub" },
                        { value: "gitlab", label: "GitLab" },
                      ]}
                    />
                    <Field label="平台 API 地址" value={String(provider.base_url ?? "")} onChange={(value) => updateDraft({ base_url: value })} help="自建 GitHub Enterprise / GitLab 时改为实际 API 地址" />
                    <Field label="Provider Token 变量名" value={String(provider.token_env ?? "")} onChange={(value) => updateDraft({ token_env: value })} help={tokenHelp} />
                  </div>
                </fieldset>
              </article>
            );
          })}
        </div>
      </section>
    </div>
  );
}

function bytesText(value?: number | null): string {
  if (value === undefined || value === null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return `${amount.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function gitCommandStatusText(state: RepositoryGitDetail["commands"][number]["state"]): string {
  return {
    waiting: "等待中",
    started: "执行中",
    progress: "执行中",
    completed: "已完成",
    failed: "失败",
    timed_out: "超时",
    cancelled: "已取消",
  }[state];
}

function RepositoryGitDetailDrawer(props: {
  repositoryId: string | null;
  detail: RepositoryGitDetail | null;
  loading: boolean;
  error: string;
  onClose: () => void;
  onOpenRun: (runId: string) => void;
}) {
  if (!props.repositoryId) return null;
  return (
    <div className="run-drawer-layer">
      <button className="run-drawer-backdrop" aria-label="关闭 Git 操作详情" onClick={props.onClose} />
      <aside className="run-drawer git-detail-drawer" role="dialog" aria-modal="true" aria-label="Git 操作详情">
        <header className="run-drawer-head">
          <div>
            <span className="eyebrow">REPOSITORY GIT</span>
            <h2>{props.repositoryId}</h2>
            <p>{props.detail?.source === "agent" ? "Agent 工作区准备" : props.detail?.source === "manual" ? "仓库页操作" : "Git 操作详情"}</p>
          </div>
          <div className="run-drawer-actions">
            {props.detail?.run_id && (
              <button className="button secondary" onClick={() => props.onOpenRun(props.detail?.run_id ?? "")}>查看 Agent 运行</button>
            )}
            <button className="run-drawer-close" aria-label="关闭" onClick={props.onClose}>×</button>
          </div>
        </header>
        <div className="run-drawer-body git-detail-body">
          {props.error && <div className="alert error">{props.error}</div>}
          {props.loading && !props.detail && <div className="empty tall"><span className="spinner" />正在读取 Git 操作…</div>}
          {props.detail && (
            <>
              <div className="git-detail-summary">
                <div><span>当前阶段</span><strong>{props.detail.phase}</strong></div>
                <div><span>开始时间</span><strong>{timeText(props.detail.started_at)}</strong></div>
                <div><span>总耗时</span><strong>{props.detail.started_at ? durationText(props.detail.started_at, props.detail.finished_at) : "—"}</strong></div>
              </div>
              <div className="git-command-list">
                {props.detail.commands.map((command, index) => (
                  <article className="git-command-card" key={command.command_id}>
                    <div className="git-command-index">{index + 1}</div>
                    <div className="git-command-content">
                      <header>
                        <strong>{command.operation}</strong>
                        <span className={`git-command-status status-${command.state}`}>{gitCommandStatusText(command.state)}</span>
                      </header>
                      {command.command && <pre>{command.command}</pre>}
                      <dl>
                        <div><dt>耗时</dt><dd>{command.elapsed_seconds} 秒</dd></div>
                        <div><dt>超时</dt><dd>{command.timeout_seconds} 秒</dd></div>
                        <div><dt>退出码</dt><dd>{command.exit_code ?? "—"}</dd></div>
                        <div><dt>完成时间</dt><dd>{timeText(command.finished_at)}</dd></div>
                      </dl>
                      {command.error && <p>{command.error}</p>}
                    </div>
                  </article>
                ))}
                {props.detail.commands.length === 0 && <div className="empty tall">还没有 Git 命令记录。</div>}
              </div>
              <p className="git-detail-safety">命令已隐藏 URL 认证信息和查询参数；不会展示原始 stdout、stderr 或 Token。</p>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

function RepositoryWorkspaceManager(props: {
  configuredRepositories: Repository[];
  draftRepositories: Repository[];
  repositoryIds?: string[];
  showHeader?: boolean;
  onOpenRun: (runId: string) => void;
}) {
  const [items, setItems] = useState<RepositoryWorkspaceStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [workingId, setWorkingId] = useState("");
  const [detailRepositoryId, setDetailRepositoryId] = useState<string | null>(null);
  const [gitDetail, setGitDetail] = useState<RepositoryGitDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");

  useBodyScrollLock(detailRepositoryId !== null);

  const refresh = useCallback(async () => {
    try {
      setItems(await api<RepositoryWorkspaceStatus[]>("/api/repositories/workspaces"));
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取基础仓库状态失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 2000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!detailRepositoryId) return;
    let cancelled = false;
    const load = async () => {
      try {
        const next = await api<RepositoryGitDetail>(
          `/api/repositories/${encodeURIComponent(detailRepositoryId)}/workspace/details`,
        );
        if (!cancelled) {
          setGitDetail(next);
          setDetailError("");
        }
      } catch (reason) {
        if (!cancelled) setDetailError(reason instanceof Error ? reason.message : "读取 Git 操作详情失败");
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    };
    setDetailLoading(true);
    void load();
    const timer = window.setInterval(() => { void load(); }, 1000);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDetailRepositoryId(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [detailRepositoryId]);

  function openDetail(repositoryId: string) {
    setGitDetail(null);
    setDetailError("");
    setDetailRepositoryId(repositoryId);
  }

  function closeDetail() {
    setDetailRepositoryId(null);
    setGitDetail(null);
    setDetailError("");
  }

  function targetChanged(repository: Repository): boolean {
    const draft = props.draftRepositories.find((item) => item.id === repository.id);
    if (!draft) return true;
    return draft.provider !== repository.provider
      || draft.project !== repository.project
      || String(draft.clone_url ?? "") !== String(repository.clone_url ?? "")
      || draft.workspace !== repository.workspace
      || (draft.enabled !== false) !== (repository.enabled !== false);
  }

  async function start(repositoryId: string) {
    setWorkingId(repositoryId);
    setError("");
    try {
      const next = await api<RepositoryWorkspaceStatus>(
        `/api/repositories/${encodeURIComponent(repositoryId)}/workspace/initialize`,
        { method: "POST" },
      );
      setItems((current) => current.map((item) => item.repository_id === repositoryId ? next : item));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "启动基础仓库操作失败");
    } finally {
      setWorkingId("");
    }
  }

  async function cancel(repositoryId: string) {
    setWorkingId(repositoryId);
    setError("");
    try {
      const next = await api<RepositoryWorkspaceStatus>(
        `/api/repositories/${encodeURIComponent(repositoryId)}/workspace/cancel`,
        { method: "POST" },
      );
      setItems((current) => current.map((item) => item.repository_id === repositoryId ? next : item));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消基础仓库操作失败");
    } finally {
      setWorkingId("");
    }
  }

  const repositoryIds = props.repositoryIds ? new Set(props.repositoryIds) : null;
  const configured = props.configuredRepositories.filter((repository) => (
    repository.enabled !== false
    && (!repositoryIds || repositoryIds.has(repository.id))
  ));
  const byId = new Map(items.map((item) => [item.repository_id, item]));
  const statusLabels: Record<RepositoryWorkspaceStatus["status"], string> = {
    uninitialized: "未初始化",
    invalid: "目录无效",
    ready: "已就绪",
    waiting: "等待仓库锁",
    initializing: "初始化中",
    updating: "更新中",
    failed: "失败",
    cancelled: "已取消",
  };

  return (
    <>
    <section className="section-card repository-workspace-manager">
      {props.showHeader !== false && (
        <div className="section-title-row">
          <div>
            <h2>基础仓库状态</h2>
            <p>初始化一次后，Agent 只增量 fetch；可写 Agent 创建独立 clone，只读 Agent 创建 linked worktree。</p>
          </div>
          <button className="button secondary" disabled={loading} onClick={() => { void refresh(); }}>刷新</button>
        </div>
      )}
      {error && <div className="alert error">{error}</div>}
      {loading && <div className="repository-workspace-empty"><span className="spinner" />正在检查基础仓库…</div>}
      {!loading && configured.length === 0 && <div className="repository-workspace-empty">当前没有已保存并启用的仓库。</div>}
      {!loading && configured.length > 0 && (
        <div className="repository-workspace-list">
          {configured.map((repository) => {
            const item = byId.get(repository.id);
            const changed = targetChanged(repository);
            const active = item ? ["waiting", "initializing", "updating"].includes(item.status) : false;
            const busy = workingId === repository.id;
            return (
              <article className="repository-workspace-row" key={repository.id}>
                <button
                  type="button"
                  className="repository-workspace-main repository-workspace-detail-trigger"
                  disabled={!item?.detail_available}
                  title={item?.detail_available ? "查看 Git 操作详情" : "还没有 Git 操作详情"}
                  onClick={() => openDetail(repository.id)}
                >
                  <div className="repository-workspace-title">
                    <strong>{repository.id}</strong>
                    {item && <span className={`repository-workspace-status status-${item.status}`}>{statusLabels[item.status]}</span>}
                    {item?.detail_source === "agent" && <span className="repository-workspace-source">Agent</span>}
                  </div>
                  <code>{item?.workspace ?? repository.workspace}</code>
                  <small>{item?.phase ?? "正在读取状态"}{item && active ? ` · ${item.elapsed_seconds} 秒` : ""}</small>
                  {changed && <small className="repository-workspace-warning">仓库目标有未保存修改，请先保存或取消编辑。</small>}
                  {item?.error && <small className="repository-workspace-error">{item.error}</small>}
                </button>
                <dl className="repository-workspace-metrics">
                  <div><dt>磁盘占用</dt><dd>{bytesText(item?.size_bytes)}</dd></div>
                  <div><dt>最近完成</dt><dd>{timeText(item?.finished_at)}</dd></div>
                </dl>
                <div className="repository-workspace-actions">
                  {active && item?.detail_source === "agent" && item.detail_run_id ? (
                    <button className="button secondary" onClick={() => props.onOpenRun(item.detail_run_id ?? "")}>查看 Agent 运行</button>
                  ) : active ? (
                    <button className="button danger" disabled={busy || item?.cancel_requested} onClick={() => { void cancel(repository.id); }}>
                      {item?.cancel_requested ? "取消中…" : "取消操作"}
                    </button>
                  ) : (
                    <button className="button primary" disabled={busy || changed || !item} onClick={() => { void start(repository.id); }}>
                      {busy ? "启动中…" : item?.ready ? "立即更新" : item?.status === "failed" || item?.status === "cancelled" ? "重新初始化" : "初始化仓库"}
                    </button>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
    <RepositoryGitDetailDrawer
      repositoryId={detailRepositoryId}
      detail={gitDetail}
      loading={detailLoading}
      error={detailError}
      onClose={closeDetail}
      onOpenRun={(runId) => {
        closeDetail();
        props.onOpenRun(runId);
      }}
    />
    </>
  );
}

function repositoryWorkspaceStatusLabel(status: RepositoryWorkspaceStatus["status"]): string {
  return {
    uninitialized: "未初始化",
    invalid: "目录无效",
    ready: "已就绪",
    waiting: "等待仓库锁",
    initializing: "初始化中",
    updating: "更新中",
    failed: "失败",
    cancelled: "已取消",
  }[status];
}

function RepositoryDetailEditor(props: {
  document: ConfigDocument;
  repositoryIndex: number;
  creating: boolean;
  disabled: boolean;
  preflightAction?: ReactNode;
  agentWorkspaceAction?: ReactNode;
  agentWorkspaceWarmup?: RepositoryWorkspaceWarmupStatus | null;
  onIdChange: (repositoryId: string) => void;
  onChange: (document: ConfigDocument) => void;
}) {
  const repository = props.document.repositories[props.repositoryIndex];
  const providerNames = Object.keys(props.document.providers);
  const protectedNames = providerCredentialNames(props.document);
  if (!repository) return <div className="empty tall">仓库配置不存在</div>;

  const preflight: Required<Omit<RepositoryPreflight, "steps">> & {
    steps: RepositoryPreflightStep[];
  } = {
    enabled: repository.preflight?.enabled ?? false,
    cache_enabled: repository.preflight?.cache_enabled ?? true,
    publish_failure_comment: repository.preflight?.publish_failure_comment ?? false,
    status_context: repository.preflight?.status_context ?? "teamwork/local-ci",
    timeout_seconds: repository.preflight?.timeout_seconds ?? 1800,
    max_output_bytes: repository.preflight?.max_output_bytes ?? 1_000_000,
    steps: repository.preflight?.steps ?? [],
  };
  const agentWorkspace: Required<Omit<RepositoryAgentWorkspace, "prepare_steps">> & {
    prepare_steps: RepositoryAgentWorkspacePrepareStep[];
  } = {
    cache_enabled: repository.agent_workspace?.cache_enabled ?? false,
    timeout_seconds: repository.agent_workspace?.timeout_seconds ?? 1800,
    max_output_bytes: repository.agent_workspace?.max_output_bytes ?? 1_000_000,
    prepare_steps: repository.agent_workspace?.prepare_steps ?? [],
  };

  function update(patch: Partial<Repository>) {
    const repositories = [...props.document.repositories];
    repositories[props.repositoryIndex] = { ...repository, ...patch };
    props.onChange({ ...props.document, repositories });
  }

  function updateId(repositoryId: string) {
    const oldDefaultWorkspace = `./workspaces/${repository.id}`;
    update({
      id: repositoryId,
      workspace: repository.workspace === oldDefaultWorkspace
        ? `./workspaces/${repositoryId}`
        : repository.workspace,
    });
    props.onIdChange(repositoryId);
  }

  function updatePreflight(patch: Partial<RepositoryPreflight>) {
    update({ preflight: { ...preflight, ...patch } });
  }

  function updateAgentWorkspace(patch: Partial<RepositoryAgentWorkspace>) {
    update({ agent_workspace: { ...agentWorkspace, ...patch } });
  }

  function updateAgentWorkspaceStep(
    index: number,
    patch: Partial<RepositoryAgentWorkspacePrepareStep>,
  ) {
    const prepare_steps = [...agentWorkspace.prepare_steps];
    prepare_steps[index] = { ...prepare_steps[index], ...patch };
    updateAgentWorkspace({ prepare_steps });
  }

  function moveAgentWorkspaceStep(index: number, offset: number) {
    const target = index + offset;
    if (target < 0 || target >= agentWorkspace.prepare_steps.length) return;
    const prepare_steps = [...agentWorkspace.prepare_steps];
    [prepare_steps[index], prepare_steps[target]] = [prepare_steps[target], prepare_steps[index]];
    updateAgentWorkspace({ prepare_steps });
  }

  function updatePreflightStep(index: number, patch: Partial<RepositoryPreflightStep>) {
    const steps = [...preflight.steps];
    steps[index] = { ...steps[index], ...patch };
    updatePreflight({ steps });
  }

  function movePreflightStep(index: number, offset: number) {
    const target = index + offset;
    if (target < 0 || target >= preflight.steps.length) return;
    const steps = [...preflight.steps];
    [steps[index], steps[target]] = [steps[target], steps[index]];
    updatePreflight({ steps });
  }

  function togglePreflight(enabled: boolean) {
    updatePreflight({
      enabled,
      steps: enabled && preflight.steps.length === 0
        ? [{ name: "repository-ci", command: ["bash", "ci/preflight.sh"] }]
        : preflight.steps,
    });
  }

  return (
    <section className="section-card repository-detail-form">
      <article className="sub-card repository-detail-card">
        <fieldset className="config-editor-surface repository-detail-config-group" disabled={props.disabled}>
          <div className="sub-card-head">
            <div><h3>{repository.id || "未命名仓库"}</h3><p>{repository.clone_url ?? repository.project}</p></div>
            <Toggle label="启用" checked={repository.enabled ?? true} onChange={(enabled) => update({ enabled })} />
          </div>
          <div className="form-grid two">
            {props.creating ? (
              <Field label="仓库 ID" value={repository.id} onChange={updateId} help="保存后作为持久身份，不允许直接修改" />
            ) : (
              <label className="field">
                <span>仓库 ID</span>
                <input value={repository.id} disabled />
                <small>关联历史事件、运行记录和临时 Git 工作区，已有仓库不可修改 ID</small>
              </label>
            )}
            <SelectField
              label="所属 GitHub / GitLab 连接"
              value={repository.provider}
              onChange={(provider) => update({ provider })}
              options={providerNames.map((provider) => ({ value: provider, label: provider }))}
              help="决定使用哪个平台 API 和 Token 扫描此仓库"
            />
            <Field
              label="远端仓库地址 / 项目路径"
              value={repository.clone_url ?? repository.project}
              onChange={(project) => update({ project, clone_url: undefined })}
              placeholder="git@github.com:owner/repository.git"
              help="支持 owner/repository、group/project、SSH 或 HTTPS Git 地址；保存后自动解析平台项目路径"
            />
            <Field
              label="基础 Git 仓库目录（自动管理）"
              value={repository.workspace}
              onChange={(workspace) => update({ workspace })}
              help="只负责克隆、校验、fetch 和运行工作区管理；Codex 不会直接在基础仓库中工作"
            />
          </div>
        </fieldset>
        <section className="repository-preflight-section">
          <div className="repository-preflight-head">
            <div>
              <strong>Agent 工作区准备</strong>
              <p>在模型启动前，于本次隔离工作区中执行用户定义的安装命令；不会修改 Prompt，也不会自动猜测包管理器。</p>
            </div>
            <div className="repository-preflight-head-actions">
              {props.agentWorkspaceAction}
            </div>
          </div>
          <fieldset className="config-editor-surface repository-detail-config-group" disabled={props.disabled}>
            <div className="repository-preflight-content">
              <div className="form-grid two">
                <Field
                  label="准备总超时（秒）"
                  type="number"
                  value={agentWorkspace.timeout_seconds}
                  onChange={(value) => updateAgentWorkspace({ timeout_seconds: Number(value) })}
                />
                <Field
                  label="最大日志字节数"
                  type="number"
                  value={agentWorkspace.max_output_bytes}
                  onChange={(value) => updateAgentWorkspace({ max_output_bytes: Number(value) })}
                />
              </div>
              <div className="repository-preflight-cache-option">
                <Toggle
                  label={agentWorkspace.cache_enabled ? "Agent 仓库级下载缓存已启用" : "Agent 仓库级下载缓存已停用"}
                  checked={agentWorkspace.cache_enabled}
                  onChange={(cache_enabled) => updateAgentWorkspace({ cache_enabled })}
                />
                <p>同一仓库跨分支共享包管理器下载缓存；准备成功后还会按命令、依赖清单和运行平台保存最多 3 份工作区快照，总上限 5 GiB。</p>
              </div>
              {props.agentWorkspaceWarmup && (
                <div className={`repository-workspace-warmup status-${props.agentWorkspaceWarmup.status}`}>
                  <div className="repository-workspace-warmup-summary">
                    <strong>{props.agentWorkspaceWarmup.phase}</strong>
                    <span>
                      {props.agentWorkspaceWarmup.snapshot_count} 份快照 · {bytesText(props.agentWorkspaceWarmup.total_size_bytes)}
                      {props.agentWorkspaceWarmup.latest?.last_used_at
                        ? ` · 最近使用 ${timeText(props.agentWorkspaceWarmup.latest.last_used_at)}`
                        : ""}
                    </span>
                  </div>
                  {props.agentWorkspaceWarmup.latest?.fingerprint && (
                    <code>指纹 {props.agentWorkspaceWarmup.latest.fingerprint.slice(0, 12)} · {props.agentWorkspaceWarmup.latest.artifact_count ?? 0} 个文件</code>
                  )}
                  {props.agentWorkspaceWarmup.error && <small>{props.agentWorkspaceWarmup.error}</small>}
                  {props.agentWorkspaceWarmup.logs.length > 0 && ["waiting", "preparing", "failed", "cancelled"].includes(props.agentWorkspaceWarmup.status) && (
                    <pre>{props.agentWorkspaceWarmup.logs.slice(-8).map((log) => {
                      const payload = typeof log.payload === "string" ? log.payload : JSON.stringify(log.payload, null, 2);
                      return `[${new Date(log.created_at * 1000).toLocaleTimeString("zh-CN")}] ${log.event_type}\n${payload}`;
                    }).join("\n")}</pre>
                  )}
                </div>
              )}
              <div className="repository-preflight-steps-head">
                <div>
                  <strong>模型启动前准备步骤</strong>
                  <p>命令完全由用户定义，按参数数组顺序直接执行且不经过 Shell；复杂流程建议调用仓库脚本。</p>
                </div>
                <button
                  type="button"
                  className="button secondary compact"
                  onClick={() => updateAgentWorkspace({
                    prepare_steps: [
                      ...agentWorkspace.prepare_steps,
                      { name: `prepare-${agentWorkspace.prepare_steps.length + 1}`, cwd: ".", command: ["npm", "ci"] },
                    ],
                  })}
                >+ 添加步骤</button>
              </div>
              <div className="repository-preflight-steps">
                {agentWorkspace.prepare_steps.map((step, index) => (
                  <article className="repository-preflight-step" key={index}>
                    <div className="repository-preflight-step-title">
                      <strong>步骤 {index + 1}</strong>
                      <div className="button-group">
                        <button type="button" className="icon-button" disabled={index === 0} title="上移" onClick={() => moveAgentWorkspaceStep(index, -1)}>↑</button>
                        <button type="button" className="icon-button" disabled={index === agentWorkspace.prepare_steps.length - 1} title="下移" onClick={() => moveAgentWorkspaceStep(index, 1)}>↓</button>
                        <button type="button" className="icon-button danger" title="删除步骤" onClick={() => updateAgentWorkspace({ prepare_steps: agentWorkspace.prepare_steps.filter((_, itemIndex) => itemIndex !== index) })}>×</button>
                      </div>
                    </div>
                    <div className="form-grid two">
                      <Field label="步骤名称" value={step.name} onChange={(name) => updateAgentWorkspaceStep(index, { name })} />
                      <Field
                        label="工作目录（仓库内相对路径）"
                        value={step.cwd ?? "."}
                        placeholder="ui"
                        onChange={(cwd) => updateAgentWorkspaceStep(index, { cwd })}
                        help="例如 ui；只允许当前 Agent 工作区内的相对目录"
                      />
                      <Field
                        label="执行程序"
                        value={step.command[0] ?? ""}
                        placeholder="npm"
                        onChange={(program) => updateAgentWorkspaceStep(index, { command: [program, ...step.command.slice(1)] })}
                        help="例如 npm、uv、python、bash"
                      />
                      <Field
                        label="单步超时（秒，可选）"
                        type="number"
                        value={step.timeout_seconds ?? ""}
                        onChange={(value) => updateAgentWorkspaceStep(index, { timeout_seconds: value ? Number(value) : undefined })}
                      />
                    </div>
                    <label className="field repository-preflight-args">
                      <span>参数（每行一个）</span>
                      <textarea
                        className="mono"
                        rows={Math.min(8, Math.max(3, step.command.length))}
                        value={step.command.slice(1).join("\n")}
                        placeholder="ci"
                        onChange={(event) => updateAgentWorkspaceStep(index, {
                          command: [step.command[0] ?? "", ...event.target.value.split("\n").filter((value) => value !== "")],
                        })}
                      />
                      <small>例如工作目录 ui、执行程序 npm、参数 ci，效果等价于在 ui 目录执行 npm ci，但不会启动 Shell。</small>
                    </label>
                  </article>
                ))}
                {agentWorkspace.prepare_steps.length === 0 && (
                  <div className="choice-empty">当前未配置准备命令；Agent 将在工作区创建后直接启动模型。</div>
                )}
              </div>
            </div>
          </fieldset>
        </section>
        <section className="repository-preflight-section">
          <div className="repository-preflight-head">
            <div>
              <strong>本地 CI 门禁</strong>
              <p>声明此仓库可执行的本地 CI，当前仅支持 GitHub。只有明确选择“执行仓库 CI”的触发规则才会使用；未配置时对应 Agent 仍会直接运行。</p>
            </div>
            <div className="repository-preflight-head-actions">
              {props.preflightAction}
              <fieldset className="config-editor-surface repository-detail-config-group" disabled={props.disabled}>
                <Toggle label={preflight.enabled ? "已启用" : "未启用"} checked={preflight.enabled} onChange={togglePreflight} />
              </fieldset>
            </div>
          </div>
          {preflight.enabled && (
            <fieldset className="config-editor-surface repository-detail-config-group" disabled={props.disabled}>
              <div className="repository-preflight-content">
                <div className="form-grid three">
                  <Field
                    label="GitHub 状态名称"
                    value={preflight.status_context}
                    onChange={(status_context) => updatePreflight({ status_context })}
                    help="建议同时配置为仓库 Ruleset 的 required status check"
                  />
                  <Field
                    label="CI 总超时（秒）"
                    type="number"
                    value={preflight.timeout_seconds}
                    onChange={(value) => updatePreflight({ timeout_seconds: Number(value) })}
                  />
                  <Field
                    label="最大日志字节数"
                    type="number"
                    value={preflight.max_output_bytes}
                    onChange={(value) => updatePreflight({ max_output_bytes: Number(value) })}
                  />
                </div>
                <div className="repository-preflight-cache-option">
                  <Toggle
                    label={preflight.cache_enabled ? "仓库级依赖缓存已启用" : "仓库级依赖缓存已停用"}
                    checked={preflight.cache_enabled}
                    onChange={(cache_enabled) => updatePreflight({ cache_enabled })}
                  />
                  <p>同一仓库的不同分支和 MR / PR 共享下载缓存；每次 CI 的工作区与安装结果仍保持隔离。覆盖 uv、pip、Poetry、PDM、npm/pnpm/Yarn、Bun、Cargo、Go、Maven、Gradle、NuGet、Composer 和常见浏览器缓存。</p>
                </div>
                <div className="repository-preflight-cache-option">
                  <Toggle
                    label="失败时发布 PR 评论"
                    checked={preflight.publish_failure_comment}
                    onChange={(publish_failure_comment) => updatePreflight({ publish_failure_comment })}
                  />
                  <p>仅自动 MR / PR CI 失败、超时或异常时创建或更新同一条评论；后续通过后删除。成功结果和手动仓库 CI 不发布评论。</p>
                </div>
                <div className="repository-preflight-steps-head">
                  <div><strong>CI 命令步骤</strong><p>按顺序直接执行参数数组，不隐式经过 shell；复杂流程建议调用仓库内脚本。</p></div>
                  <button
                    type="button"
                    className="button secondary compact"
                    onClick={() => updatePreflight({
                      steps: [...preflight.steps, { name: `step-${preflight.steps.length + 1}`, command: ["bash", "ci/preflight.sh"] }],
                    })}
                  >+ 添加步骤</button>
                </div>
                <div className="repository-preflight-steps">
                  {preflight.steps.map((step, index) => (
                    <article className="repository-preflight-step" key={index}>
                    <div className="repository-preflight-step-title">
                      <strong>步骤 {index + 1}</strong>
                      <div className="button-group">
                        <button type="button" className="icon-button" disabled={index === 0} title="上移" onClick={() => movePreflightStep(index, -1)}>↑</button>
                        <button type="button" className="icon-button" disabled={index === preflight.steps.length - 1} title="下移" onClick={() => movePreflightStep(index, 1)}>↓</button>
                        <button type="button" className="icon-button danger" title="删除步骤" onClick={() => updatePreflight({ steps: preflight.steps.filter((_, itemIndex) => itemIndex !== index) })}>×</button>
                      </div>
                    </div>
                    <div className="form-grid three">
                      <Field label="步骤名称" value={step.name} onChange={(name) => updatePreflightStep(index, { name })} />
                      <Field
                        label="执行程序"
                        value={step.command[0] ?? ""}
                        placeholder="bash"
                        onChange={(program) => updatePreflightStep(index, { command: [program, ...step.command.slice(1)] })}
                        help="例如 bash、python、npm"
                      />
                      <Field
                        label="单步超时（秒，可选）"
                        type="number"
                        value={step.timeout_seconds ?? ""}
                        onChange={(value) => updatePreflightStep(index, { timeout_seconds: value ? Number(value) : undefined })}
                      />
                    </div>
                    <label className="field repository-preflight-args">
                      <span>参数（每行一个）</span>
                      <textarea
                        className="mono"
                        rows={Math.min(8, Math.max(3, step.command.length))}
                        value={step.command.slice(1).join("\n")}
                        placeholder={"ci/preflight.sh\n--verbose"}
                        onChange={(event) => updatePreflightStep(index, {
                          command: [step.command[0] ?? "", ...event.target.value.split("\n").filter((value) => value !== "")],
                        })}
                      />
                      <small>每一行作为一个独立参数；例如 bash + ci/preflight.sh 等价于执行仓库脚本。</small>
                    </label>
                    </article>
                  ))}
                  {preflight.steps.length === 0 && <div className="choice-empty">启用本地 CI 时至少需要添加一个命令步骤。</div>}
                </div>
              </div>
            </fieldset>
          )}
        </section>
        <fieldset className="config-editor-surface repository-detail-config-group" disabled={props.disabled}>
          <EnvironmentEditor
            compact
            title="仓库环境变量"
            value={repository.environment ?? {}}
            protectedNames={protectedNames}
            onChange={(environment) => update({ environment })}
          />
        </fieldset>
      </article>
    </section>
  );
}

function RepositoriesView(props: {
  document: ConfigDocument;
  revision: string;
  onSaved: (document: ConfigDocument, revision: string) => void;
  onDirtyChange: (dirty: boolean) => void;
  onDetailOpenChange: (open: boolean) => void;
  onError: (message: string) => void;
  onNotice: (message: string) => void;
  onOpenRun: (runId: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [detailId, setDetailId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draftDocument, setDraftDocument] = useState<ConfigDocument | null>(null);
  const [draftId, setDraftId] = useState("");
  const [saving, setSaving] = useState(false);
  const [startingPreflight, setStartingPreflight] = useState(false);
  const [checkingManualPreflight, setCheckingManualPreflight] = useState(false);
  const [activeManualPreflightRunId, setActiveManualPreflightRunId] = useState<string | null>(null);
  const [selectedPreflightRunId, setSelectedPreflightRunId] = useState<string | null>(null);
  const [togglingRepositoryId, setTogglingRepositoryId] = useState<string | null>(null);
  const [workspaceItems, setWorkspaceItems] = useState<RepositoryWorkspaceStatus[]>([]);
  const [workspaceLoading, setWorkspaceLoading] = useState(true);
  const [workspaceWarmup, setWorkspaceWarmup] = useState<RepositoryWorkspaceWarmupStatus | null>(null);
  const [workspaceWarmupBusy, setWorkspaceWarmupBusy] = useState(false);
  const [pendingAction, setPendingAction] = useState<
    { kind: "discard" } | { kind: "delete" } | null
  >(null);

  const refreshWorkspaceItems = useCallback(async () => {
    try {
      setWorkspaceItems(await api<RepositoryWorkspaceStatus[]>("/api/repositories/workspaces"));
    } catch {
      // 列表状态读取失败不覆盖页面级配置错误，详情操作仍会给出明确反馈。
    } finally {
      setWorkspaceLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshWorkspaceItems();
    const timer = window.setInterval(() => { void refreshWorkspaceItems(); }, 2000);
    return () => window.clearInterval(timer);
  }, [refreshWorkspaceItems]);

  useEffect(() => {
    if (!detailId || creating) {
      setWorkspaceWarmup(null);
      return;
    }
    let disposed = false;
    const refresh = async () => {
      try {
        const status = await api<RepositoryWorkspaceWarmupStatus>(
          `/api/repositories/${encodeURIComponent(detailId)}/workspace/warmup`,
        );
        if (!disposed) setWorkspaceWarmup(status);
      } catch {
        // 轮询失败不覆盖用户正在编辑的配置，主动操作仍会显示明确错误。
      }
    };
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 1500);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [creating, detailId]);

  const findActiveManualPreflight = useCallback(async (repositoryId: string): Promise<string | null> => {
    const parameters = new URLSearchParams({
      limit: "20",
      status: "running",
      repository_id: repositoryId,
    });
    const runs = await api<PreflightRunSummary[]>(`/api/preflight-runs?${parameters.toString()}`);
    return runs.find((run) => run.trigger_source === "manual")?.run_id ?? null;
  }, []);

  useEffect(() => {
    if (!detailId || editing || creating) {
      if (!detailId) setActiveManualPreflightRunId(null);
      setCheckingManualPreflight(false);
      return;
    }
    let disposed = false;
    let firstLoad = true;
    const refresh = async () => {
      if (firstLoad) setCheckingManualPreflight(true);
      try {
        const runId = await findActiveManualPreflight(detailId);
        if (!disposed) setActiveManualPreflightRunId(runId);
      } catch {
        // 后台轮询失败不覆盖页面操作错误；启动或查看时仍会返回明确反馈。
      } finally {
        if (!disposed && firstLoad) setCheckingManualPreflight(false);
        firstLoad = false;
      }
    };
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 2000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [creating, detailId, editing, findActiveManualPreflight]);

  const repositories = useMemo(
    () => [...props.document.repositories].sort((left, right) => left.id.localeCompare(right.id)),
    [props.document.repositories],
  );
  const visibleRepositories = useMemo(() => {
    const keyword = query.trim().toLocaleLowerCase();
    if (!keyword) return repositories;
    return repositories.filter((repository) => [
      repository.id,
      repository.provider,
      repository.project,
      repository.clone_url ?? "",
      repository.workspace,
    ].some((value) => value.toLocaleLowerCase().includes(keyword)));
  }, [query, repositories]);
  const originalRepository = detailId
    ? props.document.repositories.find((repository) => repository.id === detailId)
    : undefined;
  const detailIndex = creating
    ? (draftDocument?.repositories.length ?? 0) - 1
    : detailId
      ? props.document.repositories.findIndex((repository) => repository.id === detailId)
      : -1;
  const draftRepository = detailIndex >= 0
    ? draftDocument?.repositories[detailIndex]
    : undefined;
  const dirty = editing && Boolean(
    creating || JSON.stringify(draftRepository) !== JSON.stringify(originalRepository),
  );
  const workspaceById = useMemo(
    () => new Map(workspaceItems.map((item) => [item.repository_id, item])),
    [workspaceItems],
  );
  const currentWorkspace = detailId ? workspaceById.get(detailId) : undefined;
  const gitActive = Boolean(currentWorkspace && [
    "waiting",
    "initializing",
    "updating",
  ].includes(currentWorkspace.status));
  const workspaceWarmupActive = Boolean(
    workspaceWarmup
    && ["waiting", "preparing"].includes(workspaceWarmup.status),
  );
  const referencingRules = detailId
    ? props.document.rules.filter((rule) => rule.repositories?.includes(detailId))
    : [];

  useEffect(() => {
    props.onDirtyChange(dirty);
  }, [dirty, props.onDirtyChange]);
  useEffect(() => {
    props.onDetailOpenChange(creating || detailId !== null);
  }, [creating, detailId, props.onDetailOpenChange]);
  useEffect(() => () => {
    props.onDirtyChange(false);
    props.onDetailOpenChange(false);
  }, [props.onDetailOpenChange, props.onDirtyChange]);

  function clearDraft() {
    setCreating(false);
    setEditing(false);
    setDraftDocument(null);
    setDraftId("");
  }

  function openDetail(repositoryId: string) {
    clearDraft();
    setCheckingManualPreflight(true);
    setActiveManualPreflightRunId(null);
    setDetailId(repositoryId);
  }

  function requestList() {
    if (dirty) {
      setPendingAction({ kind: "discard" });
      return;
    }
    clearDraft();
    setActiveManualPreflightRunId(null);
    setDetailId(null);
  }

  function beginCreate() {
    const providerNames = Object.keys(props.document.providers);
    if (providerNames.length === 0) return;
    let index = props.document.repositories.length + 1;
    let repositoryId = `repository-${index}`;
    while (props.document.repositories.some((repository) => repository.id === repositoryId)) {
      repositoryId = `repository-${++index}`;
    }
    const nextDocument = structuredClone(props.document);
    nextDocument.repositories.push({
      id: repositoryId,
      provider: providerNames[0],
      project: "owner/repository",
      workspace: `./workspaces/${repositoryId}`,
      enabled: true,
      environment: {},
    });
    setDetailId(null);
    setCreating(true);
    setEditing(true);
    setActiveManualPreflightRunId(null);
    setDraftId(repositoryId);
    setDraftDocument(nextDocument);
  }

  function beginEdit() {
    if (!detailId || !originalRepository || gitActive) return;
    setCreating(false);
    setEditing(true);
    setDraftId(detailId);
    setDraftDocument(structuredClone(props.document));
  }

  function cancelEdit() {
    if (creating) {
      clearDraft();
      setDetailId(null);
      return;
    }
    clearDraft();
  }

  async function saveRepository() {
    if (!editing || !draftRepository || !draftId.trim()) return;
    setSaving(true);
    props.onError("");
    try {
      const endpoint = creating
        ? "/api/config/repositories"
        : `/api/config/repositories/${encodeURIComponent(detailId ?? "")}`;
      const result = await api<{ revision: string; document: ConfigDocument }>(endpoint, {
        method: creating ? "POST" : "PUT",
        body: JSON.stringify({
          revision: props.revision,
          repository_id: draftId,
          repository: draftRepository,
        }),
      });
      const normalized = normalizeDocument(result.document);
      props.onSaved(normalized, result.revision);
      setDetailId(draftId);
      clearDraft();
      await refreshWorkspaceItems();
      props.onNotice(creating ? `仓库 ${draftId} 已创建并热加载` : `仓库 ${draftId} 已保存并热加载`);
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "保存仓库失败");
    } finally {
      setSaving(false);
    }
  }

  async function deleteRepository() {
    if (!detailId || referencingRules.length > 0 || gitActive) return;
    setSaving(true);
    props.onError("");
    try {
      const result = await api<{ revision: string; document: ConfigDocument }>(
        `/api/config/repositories/${encodeURIComponent(detailId)}`,
        {
          method: "DELETE",
          body: JSON.stringify({ revision: props.revision }),
        },
      );
      props.onSaved(normalizeDocument(result.document), result.revision);
      props.onNotice(`仓库 ${detailId} 的配置已删除，本地目录和历史记录未改动`);
      setPendingAction(null);
      setDetailId(null);
      clearDraft();
      await refreshWorkspaceItems();
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "删除仓库失败");
      setPendingAction(null);
    } finally {
      setSaving(false);
    }
  }

  async function toggleRepository(repository: Repository) {
    if (togglingRepositoryId !== null) return;
    const enabled = repository.enabled === false;
    setTogglingRepositoryId(repository.id);
    props.onError("");
    try {
      const result = await api<{ revision: string; document: ConfigDocument }>(
        `/api/config/repositories/${encodeURIComponent(repository.id)}`,
        {
          method: "PUT",
          body: JSON.stringify({
            revision: props.revision,
            repository_id: repository.id,
            repository: { ...repository, enabled },
          }),
        },
      );
      props.onSaved(normalizeDocument(result.document), result.revision);
      props.onNotice(`仓库 ${repository.id} 已${enabled ? "启用" : "停用"}并热加载`);
      await refreshWorkspaceItems();
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "更新仓库启用状态失败");
    } finally {
      setTogglingRepositoryId(null);
    }
  }

  async function startManualPreflight() {
    if (!detailId || startingPreflight || dirty) return;
    if (activeManualPreflightRunId) {
      setSelectedPreflightRunId(activeManualPreflightRunId);
      return;
    }
    setStartingPreflight(true);
    props.onError("");
    try {
      const result = await api<{
        accepted: boolean;
        run_id: string;
        reason: string;
      }>(`/api/repositories/${encodeURIComponent(detailId)}/preflight/start`, {
        method: "POST",
      });
      props.onNotice(result.reason);
      setActiveManualPreflightRunId(result.run_id);
      setSelectedPreflightRunId(result.run_id);
    } catch (reason) {
      try {
        const runId = await findActiveManualPreflight(detailId);
        if (runId) {
          setActiveManualPreflightRunId(runId);
          setSelectedPreflightRunId(runId);
          props.onNotice("已打开该仓库正在运行的手动 CI");
          return;
        }
      } catch {
        // 保留原始启动错误，避免后续查询失败覆盖真正原因。
      }
      props.onError(reason instanceof Error ? reason.message : "启动手动 CI 失败");
    } finally {
      setStartingPreflight(false);
    }
  }

  async function toggleWorkspaceWarmup() {
    if (!detailId || workspaceWarmupBusy || editing || creating) return;
    const active = workspaceWarmup && ["waiting", "preparing"].includes(workspaceWarmup.status);
    setWorkspaceWarmupBusy(true);
    props.onError("");
    try {
      const action = active ? "cancel" : "start";
      const status = await api<RepositoryWorkspaceWarmupStatus>(
        `/api/repositories/${encodeURIComponent(detailId)}/workspace/warmup/${action}`,
        { method: "POST" },
      );
      setWorkspaceWarmup(status);
      props.onNotice(active ? "已请求取消 Agent 工作区预热" : "已启动默认分支 Agent 工作区预热");
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "Agent 工作区预热操作失败");
    } finally {
      setWorkspaceWarmupBusy(false);
    }
  }

  const confirmation = useMemo<AgentActionConfirmation | null>(() => {
    if (!pendingAction) return null;
    if (pendingAction.kind === "discard") {
      return {
        eyebrow: "REPOSITORY CONFIGURATION",
        title: "放弃未保存的仓库修改？",
        description: "当前详情中的修改尚未保存，继续后无法从页面恢复。",
        details: [
          { label: "仓库", value: draftId || detailId || "新仓库", mono: true },
          { label: "配置版本", value: shortRevision(props.revision), mono: true },
        ],
        impactTitle: "只放弃当前草稿",
        impact: "已保存配置、基础仓库目录、历史事件和运行记录不会受到影响。",
        confirmLabel: "放弃修改",
        dangerous: true,
      };
    }
    if (!detailId || !originalRepository) return null;
    return {
      eyebrow: "REPOSITORY CONFIGURATION",
      title: `删除仓库 ${detailId} 的配置？`,
      description: "删除后后台将不再扫描该仓库，也不能再为它创建新的 Agent 运行。",
      details: [
        { label: "仓库", value: detailId, mono: true },
        { label: "远端项目", value: originalRepository.project, mono: true },
        { label: "基础仓库目录", value: originalRepository.workspace, mono: true },
      ],
      impactTitle: "不会删除磁盘与历史数据",
      impact: "本地基础仓库、临时 Git 工作区、SQLite 快照、事件和运行记录均会保留；如需清理应另行确认具体目标。",
      confirmLabel: "确认删除配置",
      dangerous: true,
    };
  }, [detailId, draftId, originalRepository, pendingAction, props.revision]);

  function confirmPendingAction() {
    if (pendingAction?.kind === "delete") {
      void deleteRepository();
      return;
    }
    if (pendingAction?.kind === "discard") {
      setPendingAction(null);
      clearDraft();
      setDetailId(null);
    }
  }

  if (!creating && detailId === null) {
    const hasProviders = Object.keys(props.document.providers).length > 0;
    return (
      <section className="section-card repository-list-page">
        <div className="section-title-row agent-list-title">
          <div>
            <h2>仓库</h2>
            <p>点击一行查看配置与基础仓库状态；每个仓库在详情页独立编辑和保存。</p>
          </div>
          <button
            type="button"
            className="button primary"
            disabled={!hasProviders}
            title={!hasProviders ? "请先添加 GitHub / GitLab 连接" : "添加仓库"}
            onClick={beginCreate}
          >+ 添加仓库</button>
        </div>
        <div className="agent-list-toolbar">
          <label>
            <span>搜索仓库</span>
            <input value={query} placeholder="输入仓库、平台连接或远端项目…" onChange={(event) => setQuery(event.target.value)} />
          </label>
          <span>共 {repositories.length} 个仓库</span>
        </div>
        <div className="repository-config-table">
          <div className="repository-config-table-head" aria-hidden="true">
            <span>仓库</span><span>状态</span><span>平台连接</span><span>远端项目</span><span>基础目录</span><span>仓库状态</span><span />
          </div>
          <div className="repository-config-items">
            {visibleRepositories.map((repository) => {
              const workspace = workspaceById.get(repository.id);
              const gitBusy = Boolean(workspace && ["waiting", "initializing", "updating"].includes(workspace.status));
              const enabled = repository.enabled !== false;
              const toggling = togglingRepositoryId === repository.id;
              return (
                <div
                  className="repository-config-row"
                  key={repository.id}
                  onClick={() => openDetail(repository.id)}
                >
                  <span className="agent-config-identity"><span className="repository-config-avatar" aria-hidden="true">G</span><span><strong>{repository.id}</strong><small>{repository.environment && Object.keys(repository.environment).length > 0 ? `${Object.keys(repository.environment).length} 个环境变量` : "无仓库环境变量"}</small></span></span>
                  <span className={`repository-config-status ${enabled ? "enabled" : ""}`} onClick={(event) => event.stopPropagation()}>
                    <Toggle
                      label={toggling ? "保存中…" : enabled ? "已启用" : "已停用"}
                      checked={enabled}
                      disabled={togglingRepositoryId !== null || gitBusy}
                      onChange={() => { void toggleRepository(repository); }}
                    />
                  </span>
                  <span><strong>{repository.provider}</strong></span>
                  <span className="agent-config-summary"><strong>{repository.project}</strong><small>{repository.clone_url ?? "使用平台默认克隆地址"}</small></span>
                  <span className="agent-config-summary"><strong>{repository.workspace}</strong><small>基础 Git 仓库</small></span>
                  <span className="agent-config-summary"><strong>{workspaceLoading && !workspace ? "检查中" : workspace ? repositoryWorkspaceStatusLabel(workspace.status) : "未读取"}</strong><small>{workspace?.phase ?? "等待状态检查"}</small></span>
                  <button
                    type="button"
                    className="agent-config-arrow repository-config-detail-button"
                    aria-label={`查看仓库 ${repository.id}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      openDetail(repository.id);
                    }}
                  >›</button>
                </div>
              );
            })}
            {visibleRepositories.length === 0 && (
              <div className="empty tall">
                {repositories.length === 0
                  ? hasProviders ? "尚未配置仓库" : "请先添加 GitHub / GitLab 连接"
                  : "没有匹配的仓库"}
              </div>
            )}
          </div>
        </div>
      </section>
    );
  }

  const activeDocument = editing && draftDocument ? draftDocument : props.document;
  const activeRepository = detailIndex >= 0
    ? activeDocument.repositories[detailIndex]
    : undefined;
  const activeId = creating ? draftId : detailId ?? "";
  return (
    <div className="agent-detail-page repository-detail-page">
      <header className="agent-detail-header">
        <button type="button" className="agent-detail-back" onClick={requestList}><span aria-hidden="true">←</span>返回仓库列表</button>
        <div className="agent-detail-heading">
          <div>
            <span className="eyebrow">REPOSITORY</span>
            <h2>{activeId}</h2>
            <p>配置版本 {shortRevision(props.revision)} · {creating ? "尚未保存的新仓库" : editing ? "当前修改仅保存在页面草稿" : "当前已保存配置"}</p>
          </div>
          <span className={`agent-detail-mode ${editing ? "editing" : ""}`}>{creating ? "新建" : editing ? "编辑中" : "只读"}</span>
        </div>
        <div className="button-group agent-detail-actions">
          {editing ? (
            <>
              <button type="button" className="button secondary" disabled={saving} onClick={cancelEdit}>取消</button>
              <button type="button" className="button primary" disabled={!dirty || !draftId.trim() || saving || gitActive} onClick={() => { void saveRepository(); }}>{saving ? "保存中…" : "保存仓库"}</button>
            </>
          ) : (
            <>
              <button
                type="button"
                className="button danger"
                disabled={gitActive || referencingRules.length > 0}
                title={gitActive ? "仓库正在执行 Git 操作" : referencingRules.length > 0 ? `仍被规则引用：${referencingRules.map((rule) => rule.name).join("、")}` : "删除仓库配置"}
                onClick={() => setPendingAction({ kind: "delete" })}
              >删除仓库</button>
              <button type="button" className="button primary" disabled={gitActive} title={gitActive ? "仓库正在执行 Git 操作" : "编辑仓库"} onClick={beginEdit}>编辑仓库</button>
            </>
          )}
        </div>
      </header>
      {referencingRules.length > 0 && !creating && (
        <div className="repository-reference-warning">
          <strong>当前仓库不能删除</strong>
          <span>仍被触发规则引用：{referencingRules.map((rule) => rule.name).join("、")}。请先修改这些规则的仓库范围。</span>
        </div>
      )}
      {activeRepository && (
        <RepositoryDetailEditor
          document={activeDocument}
          repositoryIndex={detailIndex}
          creating={creating}
          disabled={!editing || saving || gitActive}
          agentWorkspaceWarmup={!editing ? workspaceWarmup : null}
          agentWorkspaceAction={!editing ? (
            <button
              type="button"
              className={`button ${workspaceWarmupActive ? "danger" : "secondary"}`}
              disabled={
                workspaceWarmupBusy
                || Boolean(workspaceWarmup?.cancel_requested)
                || (!workspaceWarmupActive && (
                  gitActive
                  || originalRepository?.enabled === false
                  || !originalRepository?.agent_workspace?.cache_enabled
                  || !originalRepository?.agent_workspace?.prepare_steps?.length
                ))
              }
              title={
                workspaceWarmupActive
                  ? "取消当前 Agent 工作区预热"
                  : originalRepository?.enabled === false
                  ? "请先启用仓库"
                  : !originalRepository?.agent_workspace?.cache_enabled
                    ? "请先启用 Agent 仓库级下载缓存"
                    : !originalRepository?.agent_workspace?.prepare_steps?.length
                      ? "请先配置模型启动前准备步骤"
                      : "在远端默认分支执行准备步骤并创建可跨分支复用的依赖快照"
              }
              onClick={() => { void toggleWorkspaceWarmup(); }}
            >{workspaceWarmupBusy
              ? "处理中…"
              : workspaceWarmupActive
                ? workspaceWarmup?.cancel_requested ? "取消中…" : "取消预热"
                : workspaceWarmup?.status === "ready" ? "重新预热" : "预热准备"}</button>
          ) : undefined}
          preflightAction={!editing ? (
            <button
              type="button"
              className="button secondary"
              disabled={
                checkingManualPreflight
                || (!activeManualPreflightRunId && (
                  startingPreflight
                  || gitActive
                  || originalRepository?.enabled === false
                  || !originalRepository?.preflight?.enabled
                  || !originalRepository?.preflight?.steps?.length
                ))
              }
              title={
                activeManualPreflightRunId
                  ? "重新打开该仓库正在运行的手动 CI"
                  : originalRepository?.enabled === false
                  ? "请先启用仓库"
                  : !originalRepository?.preflight?.enabled
                    ? "请先配置并启用本地 CI"
                    : "不限时执行远端默认分支最新提交，用于验证 CI 并预热仓库级缓存"
              }
              onClick={() => { void startManualPreflight(); }}
            >{checkingManualPreflight
              ? "检查运行状态…"
              : activeManualPreflightRunId
                ? "查看运行中的 CI"
                : startingPreflight ? "启动中…" : "执行 CI / 预热缓存"}</button>
          ) : undefined}
          onIdChange={setDraftId}
          onChange={setDraftDocument}
        />
      )}
      {creating ? (
        <section className="section-card repository-workspace-manager">
          <div className="section-title-row"><div><h2>基础仓库状态</h2><p>保存仓库配置后才能初始化基础 Git 仓库。</p></div></div>
          <div className="repository-workspace-empty">当前仓库尚未保存。</div>
        </section>
      ) : (
        <RepositoryWorkspaceManager
          configuredRepositories={props.document.repositories}
          draftRepositories={activeDocument.repositories}
          repositoryIds={detailId ? [detailId] : []}
          onOpenRun={(runId) => {
            if (dirty) {
              props.onError("请先保存或取消当前仓库修改，再打开 Agent 运行");
              return;
            }
            props.onOpenRun(runId);
          }}
        />
      )}
      <AgentActionConfirmationDialog
        model={confirmation}
        busy={saving}
        onCancel={() => { if (!saving) setPendingAction(null); }}
        onConfirm={confirmPendingAction}
      />
      {selectedPreflightRunId && (
        <PreflightRunDetailDrawer
          runId={selectedPreflightRunId}
          depth={0}
          onClose={() => {
            setSelectedPreflightRunId(null);
            if (detailId) {
              void findActiveManualPreflight(detailId)
                .then(setActiveManualPreflightRunId)
                .catch(() => undefined);
            }
          }}
        />
      )}
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
  visibleNames?: string[];
  showOverview?: boolean;
  allowDelete?: boolean;
  onRename?: (name: string) => void;
}) {
  const allNames = Object.keys(props.document.agents);
  const names = props.visibleNames ?? allNames;
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
    props.onRename?.(nextName);
    return true;
  }
  return (
    <section className={`section-card ${props.showOverview === false ? "agent-detail-form" : ""}`}>
      {props.showOverview !== false && <div className="section-title-row">
        <div><h2>Agent</h2><p>每个 Agent 可独立选择模型 Provider，也可以继承全局默认模型。</p></div>
        <button className="button primary" onClick={() => {
          let index = allNames.length + 1;
          let name = `agent-${index}`;
          while (props.document.agents[name]) name = `agent-${++index}`;
          props.onChange({ ...props.document, agents: { ...props.document.agents, [name]: createEmptyAgent() } });
        }}>+ 添加 Agent</button>
      </div>}
      {props.showOverview !== false && <div className="agent-workspace-note">
        <strong>每次运行使用独立 Git 工作区</strong>
        <span>Agent 本身不绑定固定目录；仓库配置目录只作基础仓库。可写 Agent 使用自带 .git 的独立 clone，只读 Agent 使用轻量 linked worktree；不同分支可以并发，声明“本地仓库写操作”后同一源分支会串行。sub-agent 是否复用父 Agent 当前工作区，由触发规则中的“继承当前工作区”决定。</span>
      </div>}
      <div className="card-list">
        {names.map((name) => {
          const agent = props.document.agents[name];
          const globalDefault = props.document.runtime.default_model ?? { provider: "codex-cli" };
          const selectedProviderId = agent.model_provider ?? globalDefault.provider;
          const selectedProvider = props.document.model_providers[selectedProviderId];
          const selectedProviderModel = resolvedProviderModel(
            props.document,
            props.codexOptions,
            selectedProviderId,
            agent.model ?? (agent.model_provider ? undefined : globalDefault.model),
          );
          const agentModel = selectedProviderModel.model || inheritedModel.value;
          const inheritedReasoning = runtimeInheritedSetting(
            props.document,
            props.codexOptions,
            "model_reasoning_effort",
            agentModel,
            agent.sandbox,
            selectedProvider,
          );
          const inheritedFastMode = runtimeInheritedSetting(
            props.document,
            props.codexOptions,
            "fast_mode",
            agentModel,
            agent.sandbox,
            selectedProvider,
          );
          const inheritedVerbosity = runtimeInheritedSetting(
            props.document,
            props.codexOptions,
            "model_verbosity",
            agentModel,
            agent.sandbox,
            selectedProvider,
          );
          const inheritedPersonality = runtimeInheritedSetting(
            props.document,
            props.codexOptions,
            "personality",
            agentModel,
            agent.sandbox,
            selectedProvider,
          );
          const inheritedWebSearch = runtimeInheritedSetting(
            props.document,
            props.codexOptions,
            "web_search",
            agentModel,
            agent.sandbox,
            selectedProvider,
          );
          const promptSource = agent.prompt_file ? "file" : "inline";
          const writeScopes = agent.write_scopes ?? [];
          const subAgentOptions = allNames
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
                {props.allowDelete !== false && <button className="icon-button danger" onClick={() => {
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
                }}>×</button>}
              </div>
              <div className="form-grid three">
                <SelectField
                  label="模型 Provider"
                  value={agent.model_provider ?? ""}
                  onChange={(model_provider) => update(name, {
                    model_provider: model_provider || undefined,
                    model: undefined,
                  })}
                  options={[
                    {
                      value: "",
                      label: agentModelDisplay(props.document, props.codexOptions, {}),
                    },
                    ...Object.entries(props.document.model_providers).map(([id, provider]) => ({
                      value: id,
                      label: `${provider.display_name}${provider.enabled === false ? "（已停用）" : ""}`,
                    })),
                  ]}
                  help={agentModelDisplay(props.document, props.codexOptions, agent)}
                />
                {agent.model_provider ? (
                  <ModelField
                    id={`agent-models-${name.replace(/[^A-Za-z0-9_-]/g, "-")}`}
                    label="模型（可选）"
                    value={agent.model ?? ""}
                    placeholder={selectedProviderModel.defaultLabel}
                    models={selectedProviderModel.models}
                    onChange={(model) => update(name, { model: model || undefined })}
                    help={agentModelDisplay(props.document, props.codexOptions, agent)}
                  />
                ) : (
                  <Field
                    label="当前有效模型"
                    value={agentModelDisplay(props.document, props.codexOptions, agent)}
                    onChange={() => undefined}
                    disabled
                    help="未指定模型时始终继承全局默认 Provider 与模型"
                  />
                )}
                <SelectField
                  label="本地文件权限（Sandbox）"
                  value={agent.sandbox ?? "read-only"}
                  onChange={(value) => {
                    const sandbox = value as Agent["sandbox"];
                    const nextScopes = sandbox === "read-only"
                      ? writeScopes.filter((scope) => scope !== "workspace")
                      : Array.from(new Set([...writeScopes, "workspace"]));
                    update(name, {
                      sandbox,
                      home_mode: sandbox === "read-only" ? "inherit" : agent.home_mode ?? "inherit",
                      write_scopes: nextScopes as Agent["write_scopes"],
                      network_access: sandbox === "danger-full-access"
                        ? true
                        : sandbox === "read-only" || agent.sandbox === "danger-full-access"
                          ? false
                          : agent.network_access ?? false,
                      network_domains: sandbox === "workspace-write" ? agent.network_domains ?? [] : [],
                    });
                  }}
                  options={[
                    { value: "read-only", label: "只读：不能修改本地文件" },
                    { value: "workspace-write", label: "工作区可写：可修改仓库文件和独立 .git" },
                    { value: "danger-full-access", label: "完全访问：绕过受限外层沙盒（高风险）" },
                  ]}
                  help="可写模式放行本次独立 clone（含 .git）和系统临时目录，基础仓库不在可写范围；切换时会自动启用“本地仓库写操作”"
                />
                <SelectField
                  label="HOME 目录"
                  value={agent.home_mode ?? "inherit"}
                  disabled={agent.sandbox === "read-only"}
                  onChange={(homeMode) => update(name, { home_mode: homeMode as Agent["home_mode"] })}
                  options={[
                    { value: "inherit", label: "继承系统 HOME" },
                    { value: "temporary", label: "每次运行使用临时 HOME" },
                  ]}
                  help={agent.sandbox === "read-only"
                    ? "只读沙箱不能提供可写的临时 HOME"
                    : agent.home_mode === "temporary"
                      ? "缓存与用户级配置写入本次运行目录，结束后清理"
                      : "命令继续直接使用启动服务用户的 HOME"}
                />
                <Field label="总超时（秒）" type="number" value={agent.timeout_seconds ?? 1200} onChange={(value) => update(name, { timeout_seconds: Number(value) })} />
                <Field label="无进展超时（秒，可选）" type="number" value={agent.idle_timeout_seconds ?? ""} placeholder={`继承运行时默认 ${String(props.document.runtime.agent_idle_timeout_seconds ?? 300)}`} onChange={(value) => update(name, { idle_timeout_seconds: value ? Number(value) : undefined })} />
                <Field label="此 Agent 并发数（可选）" type="number" value={agent.max_concurrent_runs ?? ""} placeholder="留空表示不额外限制" onChange={(value) => update(name, { max_concurrent_runs: value ? Number(value) : undefined })} help="根 Agent 与同名 sub-agent 共同计数，仍受全局和运行时总额度限制" />
                <Field label="输出 Schema（可选）" value={agent.output_schema ?? ""} onChange={(output_schema) => update(name, { output_schema: output_schema || undefined })} />
              </div>
              {props.showOverview === false && (
                <div className="agent-workspace-note">
                  <strong>当前继承值</strong>
                  <span>选择框会显示运行时默认解析到的具体值；无法可靠确定时显示 UNK。仓库 `.codex/config.toml` 仍可能让实际运行值随仓库变化。</span>
                </div>
              )}
              <div className="form-grid three agent-runtime-overrides">
                <SelectField
                  label="推理强度（可选）"
                  value={agent.model_reasoning_effort ?? ""}
                  onChange={(value) => update(name, { model_reasoning_effort: value || undefined })}
                  options={[
                    {
                      value: "",
                      label: agentInheritanceLabel(
                        "model_reasoning_effort",
                        inheritedReasoning,
                      ),
                    },
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
                    {
                      value: "inherit",
                      label: agentInheritanceLabel("fast_mode", inheritedFastMode),
                    },
                    { value: "standard", label: "标准模式" },
                    { value: "fast", label: "快速模式" },
                  ]}
                />
                <SelectField
                  label="输出详细度（可选）"
                  value={agent.model_verbosity ?? ""}
                  onChange={(value) => update(name, { model_verbosity: value ? value as Agent["model_verbosity"] : undefined })}
                  options={[
                    {
                      value: "",
                      label: agentInheritanceLabel("model_verbosity", inheritedVerbosity),
                    },
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
                    {
                      value: "",
                      label: agentInheritanceLabel("personality", inheritedPersonality),
                    },
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
                    {
                      value: "",
                      label: agentInheritanceLabel("web_search", inheritedWebSearch),
                    },
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
              <section className="network-permission-section">
                <div className="network-permission-head">
                  <div>
                    <strong>按源版本托管顶层评论</strong>
                    <p>目标分支变化时更新当前评论；源分支出现新提交或 force-push 时，为新的时间线代次追加评论。</p>
                  </div>
                  <div className="managed-comment-toggles">
                    <Toggle
                      label="每个源版本代次只维护一条评论"
                      checked={agent.managed_comment ?? false}
                      onChange={(managed_comment) => update(name, {
                        managed_comment,
                        managed_comment_slot: managed_comment
                          ? agent.managed_comment_slot ?? crypto.randomUUID()
                          : agent.managed_comment_slot,
                        write_scopes: managed_comment
                          ? Array.from(new Set([...writeScopes, "change_request"])) as Agent["write_scopes"]
                          : writeScopes,
                      })}
                    />
                    <Toggle
                      label="附加模型签名"
                      checked={agent.managed_comment_model_signature ?? false}
                      disabled={!(agent.managed_comment ?? false)}
                      onChange={(managed_comment_model_signature) => update(name, { managed_comment_model_signature })}
                    />
                  </div>
                </div>
                <p className="network-permission-state">
                  开启后，最终顶层评论必须调用 <code>publish_comment</code> 工具进行发布或更新；模型签名由服务端根据本轮运行快照自动追加。关闭不会删除历史评论。人工删除远端评论后，只有下次实际发布时才会重新创建。
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
                    const hasChangeRequest = values.includes("change_request");
                    update(name, {
                      write_scopes: values as Agent["write_scopes"],
                      managed_comment: hasChangeRequest
                        ? agent.managed_comment ?? false
                        : false,
                      sandbox: hasWorkspace
                        ? agent.sandbox === "danger-full-access" ? "danger-full-access" : "workspace-write"
                        : "read-only",
                      home_mode: hasWorkspace ? agent.home_mode ?? "inherit" : "inherit",
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
                  <div className="prompt-source-picker">
                    <span>Prompt 来源</span>
                    <SelectControl
                      value={promptSource}
                      ariaLabel="Prompt 来源"
                      onChange={(value) => update(name, value === "file"
                        ? { prompt_file: "./prompts/agent.md", prompt: undefined }
                        : { prompt: "请处理当前 MR / PR。", prompt_file: undefined })}
                      options={[
                        { value: "file", label: "文件" },
                        { value: "inline", label: "内联模板" },
                      ]}
                    />
                  </div>
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
              <p className="network-credential-note">
                临时 HOME 与独立 Git 工作区相互独立：运行工作区隔离仓库文件和 Git 元数据，临时 HOME 隔离 <code>~/.cache</code>、<code>~/.config</code> 等用户级写入。Codex Home 与已存在的 gh / glab / Git / SSH 登录入口会单独桥接，Provider Token 仍不会进入 Agent。
              </p>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function agentSandboxLabel(sandbox: Agent["sandbox"]): string {
  if (sandbox === "workspace-write") return "工作区可写";
  if (sandbox === "danger-full-access") return "完全访问";
  return "只读";
}

function agentNetworkLabel(agent: Agent): string {
  if (agent.sandbox === "danger-full-access") return "不受限制";
  if (!agent.network_access) return "关闭";
  const domainCount = agent.network_domains?.length ?? 0;
  return domainCount > 0 ? `${domainCount} 个域名` : "允许联网";
}

function agentHomeLabel(agent: Agent): string {
  return agent.home_mode === "temporary" ? "临时 HOME" : "系统 HOME";
}

function AgentsView(props: {
  document: ConfigDocument;
  revision: string;
  codexOptions: CodexRuntimeOptions;
  onSaved: (document: ConfigDocument, revision: string) => void;
  onDirtyChange: (dirty: boolean) => void;
  onError: (message: string) => void;
  onNotice: (message: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [detailName, setDetailName] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draftDocument, setDraftDocument] = useState<ConfigDocument | null>(null);
  const [draftName, setDraftName] = useState("");
  const [saving, setSaving] = useState(false);
  const [pendingAction, setPendingAction] = useState<
    { kind: "discard"; nextName: string | null } | { kind: "delete" } | null
  >(null);

  const names = useMemo(
    () => Object.keys(props.document.agents).sort((left, right) => left.localeCompare(right)),
    [props.document.agents],
  );
  const visibleNames = useMemo(() => {
    const keyword = query.trim().toLocaleLowerCase();
    return keyword
      ? names.filter((name) => name.toLocaleLowerCase().includes(keyword))
      : names;
  }, [names, query]);
  const originalAgent = detailName ? props.document.agents[detailName] : undefined;
  const draftAgent = draftDocument?.agents[draftName];
  const dirty = editing && Boolean(
    creating
    || draftName !== detailName
    || JSON.stringify(draftAgent) !== JSON.stringify(originalAgent),
  );

  useEffect(() => {
    props.onDirtyChange(dirty);
  }, [dirty, props.onDirtyChange]);
  useEffect(() => () => props.onDirtyChange(false), [props.onDirtyChange]);

  function clearDraft() {
    setCreating(false);
    setEditing(false);
    setDraftDocument(null);
    setDraftName("");
  }

  function openDetail(name: string) {
    clearDraft();
    setDetailName(name);
  }

  function requestDetail(name: string | null) {
    if (dirty) {
      setPendingAction({ kind: "discard", nextName: name });
      return;
    }
    if (name) openDetail(name);
    else {
      clearDraft();
      setDetailName(null);
    }
  }

  function beginCreate() {
    let index = names.length + 1;
    let name = `agent-${index}`;
    while (props.document.agents[name]) name = `agent-${++index}`;
    const nextDocument = structuredClone(props.document);
    nextDocument.agents[name] = createEmptyAgent();
    setDetailName(null);
    setCreating(true);
    setEditing(true);
    setDraftName(name);
    setDraftDocument(nextDocument);
  }

  function beginEdit() {
    if (!detailName || !props.document.agents[detailName]) return;
    setCreating(false);
    setEditing(true);
    setDraftName(detailName);
    setDraftDocument(structuredClone(props.document));
  }

  function cancelEdit() {
    if (creating) {
      clearDraft();
      setDetailName(null);
      return;
    }
    clearDraft();
  }

  async function saveAgent() {
    if (!editing || !draftDocument || !draftAgent || !draftName.trim()) return;
    setSaving(true);
    props.onError("");
    try {
      const endpoint = creating
        ? "/api/config/agents"
        : `/api/config/agents/${encodeURIComponent(detailName ?? "")}`;
      const result = await api<{ revision: string; document: ConfigDocument }>(endpoint, {
        method: creating ? "POST" : "PUT",
        body: JSON.stringify({
          revision: props.revision,
          name: draftName,
          agent: draftAgent,
        }),
      });
      const normalized = normalizeDocument(result.document);
      props.onSaved(normalized, result.revision);
      setDetailName(draftName);
      clearDraft();
      props.onNotice(creating ? `Agent ${draftName} 已创建并热加载` : `Agent ${draftName} 已保存并热加载`);
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "保存 Agent 失败");
    } finally {
      setSaving(false);
    }
  }

  async function deleteAgent() {
    if (!detailName) return;
    setSaving(true);
    props.onError("");
    try {
      const result = await api<{ revision: string; document: ConfigDocument }>(
        `/api/config/agents/${encodeURIComponent(detailName)}`,
        {
          method: "DELETE",
          body: JSON.stringify({ revision: props.revision }),
        },
      );
      props.onSaved(normalizeDocument(result.document), result.revision);
      props.onNotice(`Agent ${detailName} 已删除，相关规则与 sub-agent 引用已清理`);
      setPendingAction(null);
      setDetailName(null);
      clearDraft();
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "删除 Agent 失败");
      setPendingAction(null);
    } finally {
      setSaving(false);
    }
  }

  const confirmation = useMemo<AgentActionConfirmation | null>(() => {
    if (!pendingAction) return null;
    if (pendingAction.kind === "discard") {
      return {
        title: "放弃未保存的 Agent 修改？",
        description: "当前详情中的修改尚未保存，继续后无法从页面恢复。",
        details: [
          { label: "Agent", value: draftName || detailName || "新 Agent", mono: true },
          { label: "配置版本", value: shortRevision(props.revision), mono: true },
        ],
        impactTitle: "只放弃当前草稿",
        impact: "已保存配置和正在运行的 Agent 不会受到影响。",
        confirmLabel: "放弃修改",
        dangerous: true,
      };
    }
    if (!detailName) return null;
    const ruleCount = props.document.rules.filter((rule) => rule.agents.includes(detailName)).length;
    const callerCount = Object.entries(props.document.agents).filter(
      ([name, agent]) => name !== detailName && agent.allowed_sub_agents?.includes(detailName),
    ).length;
    return {
      title: `删除 Agent ${detailName}？`,
      description: "删除后会立即保存配置并通知后台热加载。",
      details: [
        { label: "Agent", value: detailName, mono: true },
        { label: "触发规则引用", value: `${ruleCount} 条` },
        { label: "sub-agent 引用", value: `${callerCount} 个` },
      ],
      impactTitle: "同步清理名称引用",
      impact: "引用该 Agent 的触发规则和其他 Agent 白名单会同时移除对应名称；历史运行记录不会删除。",
      confirmLabel: "确认删除",
      dangerous: true,
    };
  }, [detailName, draftName, pendingAction, props.document.agents, props.document.rules, props.revision]);

  function confirmPendingAction() {
    if (pendingAction?.kind === "delete") {
      void deleteAgent();
      return;
    }
    if (pendingAction?.kind === "discard") {
      const nextName = pendingAction.nextName;
      setPendingAction(null);
      if (nextName) openDetail(nextName);
      else {
        clearDraft();
        setDetailName(null);
      }
    }
  }

  const detailOpen = creating || detailName !== null;
  if (!detailOpen) {
    return (
      <section className="section-card agent-list-page">
        <div className="section-title-row agent-list-title">
          <div>
            <h2>Agent</h2>
            <p>点击一行查看完整配置；每个 Agent 在详情页独立编辑和保存。</p>
          </div>
          <button type="button" className="button primary" onClick={beginCreate}>+ 添加 Agent</button>
        </div>
        <div className="agent-workspace-note">
          <strong>每次运行使用独立 Git 工作区</strong>
          <span>Agent 不绑定固定目录；文件权限、HOME 模式、命令联网、Skill 与 sub-agent 白名单均在各自详情中独立配置。</span>
        </div>
        <div className="agent-list-toolbar">
          <label>
            <span>搜索 Agent</span>
            <input value={query} placeholder="输入 Agent 名称…" onChange={(event) => setQuery(event.target.value)} />
          </label>
          <span>共 {names.length} 个 Agent</span>
        </div>
        <div className="agent-config-table">
          <div className="agent-config-table-head" aria-hidden="true">
            <span>Agent</span><span>Prompt / 模型</span><span>本地文件权限</span><span>命令联网</span><span>Skill</span><span>Sub-agent</span><span />
          </div>
          <div className="agent-config-items">
            {visibleNames.map((name) => {
              const agent = props.document.agents[name];
              return (
                <button type="button" className="agent-config-row" key={name} onClick={() => requestDetail(name)}>
                  <span className="agent-config-identity"><span className="agent-config-avatar" aria-hidden="true">A</span><span><strong>{name}</strong><small>Codex Agent</small></span></span>
                  <span className="agent-config-summary"><strong>{agent.prompt_file ? "文件 Prompt" : "内联 Prompt"}</strong><small>{agentModelDisplay(props.document, props.codexOptions, agent)}</small></span>
                  <span><strong>{agentSandboxLabel(agent.sandbox)}</strong><small>{agentHomeLabel(agent)}</small></span>
                  <span><strong className={agent.network_access || agent.sandbox === "danger-full-access" ? "success-text" : ""}>{agentNetworkLabel(agent)}</strong></span>
                  <span><strong>{agent.skills?.length ?? 0}</strong><small>项</small></span>
                  <span><strong>{agent.allowed_sub_agents?.length ?? 0}</strong><small>个</small></span>
                  <span className="agent-config-arrow" aria-hidden="true">›</span>
                </button>
              );
            })}
            {visibleNames.length === 0 && <div className="empty tall">{names.length === 0 ? "尚未配置 Agent" : "没有匹配的 Agent"}</div>}
          </div>
        </div>
      </section>
    );
  }

  const activeDocument = editing && draftDocument ? draftDocument : props.document;
  const activeName = editing ? draftName : detailName ?? "";
  return (
    <div className="agent-detail-page">
      <header className="agent-detail-header">
        <button type="button" className="agent-detail-back" onClick={() => requestDetail(null)}><span aria-hidden="true">←</span>返回 Agent 列表</button>
        <div className="agent-detail-heading">
          <div>
            <span className="eyebrow">CODEX AGENT</span>
            <h2>{activeName}</h2>
            <p>配置版本 {shortRevision(props.revision)} · {creating ? "尚未保存的新 Agent" : editing ? "当前修改仅保存在页面草稿" : "当前已保存配置"}</p>
          </div>
          <span className={`agent-detail-mode ${editing ? "editing" : ""}`}>{creating ? "新建" : editing ? "编辑中" : "只读"}</span>
        </div>
        <div className="button-group agent-detail-actions">
          {editing ? (
            <>
              <button type="button" className="button secondary" disabled={saving} onClick={cancelEdit}>取消</button>
              <button type="button" className="button primary" disabled={!dirty || saving} onClick={() => { void saveAgent(); }}>{saving ? "保存中…" : "保存 Agent"}</button>
            </>
          ) : (
            <>
              <button type="button" className="button danger" onClick={() => setPendingAction({ kind: "delete" })}>删除 Agent</button>
              <button type="button" className="button primary" onClick={beginEdit}>编辑 Agent</button>
            </>
          )}
        </div>
      </header>
      <fieldset className="config-editor-surface agent-detail-surface" disabled={!editing || saving}>
        <AgentsEditor
          document={activeDocument}
          codexOptions={props.codexOptions}
          visibleNames={[activeName]}
          showOverview={false}
          allowDelete={false}
          onRename={setDraftName}
          onChange={setDraftDocument}
        />
      </fieldset>
      <AgentActionConfirmationDialog
        model={confirmation}
        busy={saving}
        onCancel={() => { if (!saving) setPendingAction(null); }}
        onConfirm={confirmPendingAction}
      />
    </div>
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

function createEmptyRule(document: ConfigDocument, events: string[]): Rule {
  let index = document.rules.length + 1;
  let name = `rule-${index}`;
  const existingNames = new Set(document.rules.map((rule) => rule.name));
  while (existingNames.has(name)) name = `rule-${++index}`;
  return {
    name,
    events: [events[0] ?? "change_request.updated"],
    agents: Object.keys(document.agents).slice(0, 1),
    conditions: {},
    deduplicate_per_scan: false,
    inherit_workspace: false,
    run_preflight: false,
    enabled: true,
  };
}

function RulesEditor(props: {
  document: ConfigDocument;
  events: string[];
  visibleIndexes?: number[];
  showOverview?: boolean;
  allowDelete?: boolean;
  onRename?: (name: string) => void;
  onChange: (document: ConfigDocument) => void;
}) {
  const agentNames = Object.keys(props.document.agents);
  const repositoryNames = props.document.repositories.map((repository) => repository.id);
  const visibleIndexes = props.visibleIndexes ? new Set(props.visibleIndexes) : null;
  const entries = props.document.rules
    .map((rule, index) => ({ rule, index }))
    .filter(({ index }) => !visibleIndexes || visibleIndexes.has(index));

  function update(index: number, patch: Partial<Rule>) {
    const rules = [...props.document.rules];
    rules[index] = { ...rules[index], ...patch };
    props.onChange({ ...props.document, rules });
    if (patch.name !== undefined) props.onRename?.(patch.name);
  }

  function addRule() {
    const rule = createEmptyRule(props.document, props.events);
    props.onChange({ ...props.document, rules: [...props.document.rules, rule] });
  }

  return (
    <section className={`section-card ${props.showOverview === false ? "rule-detail-form" : ""}`}>
      {props.showOverview !== false && (
        <div className="section-title-row">
          <div><h2>MR / PR 触发规则</h2><p>状态变化生成事件，匹配规则后按顺序触发所选 Agent。</p></div>
          <button type="button" className="button primary" onClick={addRule}>+ 添加规则</button>
        </div>
      )}
      <div className="card-list">
        {entries.map(({ rule, index }) => (
          <article className="sub-card rule-card" key={index}>
            <div className="sub-card-head">
              <div><h3>{rule.name}</h3><p>{rule.events.length} 个事件 · {rule.agents.length} 个 Agent</p></div>
              <div className="button-group">
                <Toggle label="启用" checked={rule.enabled ?? true} onChange={(enabled) => update(index, { enabled })} />
                {props.allowDelete !== false && (
                  <button
                    type="button"
                    className="icon-button danger"
                    onClick={() => props.onChange({
                      ...props.document,
                      rules: props.document.rules.filter((_, itemIndex) => itemIndex !== index),
                    })}
                  >×</button>
                )}
              </div>
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
                  label="执行仓库 CI（如已启用）"
                  checked={rule.run_preflight ?? false}
                  onChange={(run_preflight) => update(index, { run_preflight })}
                />
                <p>仓库已配置本地 CI 且 PR 仍打开时先执行门禁；仓库未配置或 PR 已关闭、合并时直接运行 Agent，不报错。</p>
              </div>
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
                <p>开启后，sub-agent 复用父 Agent 本次运行的临时 clone 或 worktree，共享当前分支、暂存区和未提交文件；只继承工作区，不自动继承 MR 输入或父 Agent 对话。</p>
              </div>
            </div>
            <JsonEditor value={rule.conditions ?? {}} onChange={(conditions) => update(index, { conditions })} />
          </article>
        ))}
      </div>
    </section>
  );
}

function RulesView(props: {
  document: ConfigDocument;
  revision: string;
  events: string[];
  onSaved: (document: ConfigDocument, revision: string) => void;
  onDirtyChange: (dirty: boolean) => void;
  onError: (message: string) => void;
  onNotice: (message: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [detailName, setDetailName] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draftDocument, setDraftDocument] = useState<ConfigDocument | null>(null);
  const [draftName, setDraftName] = useState("");
  const [saving, setSaving] = useState(false);
  const [togglingRuleName, setTogglingRuleName] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<
    { kind: "discard"; nextName: string | null } | { kind: "delete" } | null
  >(null);

  const rules = useMemo(
    () => [...props.document.rules].sort((left, right) => left.name.localeCompare(right.name)),
    [props.document.rules],
  );
  const visibleRules = useMemo(() => {
    const keyword = query.trim().toLocaleLowerCase();
    if (!keyword) return rules;
    return rules.filter((rule) => [
      rule.name,
      ...rule.events,
      ...rule.agents,
      ...(rule.repositories ?? []),
    ].some((value) => value.toLocaleLowerCase().includes(keyword)));
  }, [query, rules]);
  const originalRule = detailName
    ? props.document.rules.find((rule) => rule.name === detailName)
    : undefined;
  const detailIndex = creating
    ? (draftDocument?.rules.length ?? 0) - 1
    : detailName
      ? props.document.rules.findIndex((rule) => rule.name === detailName)
      : -1;
  const draftRule = detailIndex >= 0 ? draftDocument?.rules[detailIndex] : undefined;
  const dirty = editing && Boolean(
    creating
    || draftName !== detailName
    || JSON.stringify(draftRule) !== JSON.stringify(originalRule),
  );

  useEffect(() => {
    props.onDirtyChange(dirty);
  }, [dirty, props.onDirtyChange]);
  useEffect(() => () => props.onDirtyChange(false), [props.onDirtyChange]);

  function clearDraft() {
    setCreating(false);
    setEditing(false);
    setDraftDocument(null);
    setDraftName("");
  }

  function openDetail(name: string) {
    clearDraft();
    setDetailName(name);
  }

  function requestDetail(name: string | null) {
    if (dirty) {
      setPendingAction({ kind: "discard", nextName: name });
      return;
    }
    if (name) openDetail(name);
    else {
      clearDraft();
      setDetailName(null);
    }
  }

  function beginCreate() {
    const rule = createEmptyRule(props.document, props.events);
    const nextDocument = structuredClone(props.document);
    nextDocument.rules.push(rule);
    setDetailName(null);
    setCreating(true);
    setEditing(true);
    setDraftName(rule.name);
    setDraftDocument(nextDocument);
  }

  function beginEdit() {
    if (!detailName || !originalRule) return;
    setCreating(false);
    setEditing(true);
    setDraftName(detailName);
    setDraftDocument(structuredClone(props.document));
  }

  function cancelEdit() {
    if (creating) {
      clearDraft();
      setDetailName(null);
      return;
    }
    clearDraft();
  }

  async function saveRule() {
    if (!editing || !draftDocument || !draftRule || !draftName.trim()) return;
    setSaving(true);
    props.onError("");
    try {
      const endpoint = creating
        ? "/api/config/rules"
        : `/api/config/rules/${encodeURIComponent(detailName ?? "")}`;
      const result = await api<{ revision: string; document: ConfigDocument }>(endpoint, {
        method: creating ? "POST" : "PUT",
        body: JSON.stringify({
          revision: props.revision,
          name: draftName,
          rule: draftRule,
        }),
      });
      const normalized = normalizeDocument(result.document);
      props.onSaved(normalized, result.revision);
      setDetailName(draftName);
      clearDraft();
      props.onNotice(creating ? `规则 ${draftName} 已创建并热加载` : `规则 ${draftName} 已保存并热加载`);
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "保存规则失败");
    } finally {
      setSaving(false);
    }
  }

  async function deleteRule() {
    if (!detailName) return;
    setSaving(true);
    props.onError("");
    try {
      const result = await api<{ revision: string; document: ConfigDocument }>(
        `/api/config/rules/${encodeURIComponent(detailName)}`,
        {
          method: "DELETE",
          body: JSON.stringify({ revision: props.revision }),
        },
      );
      props.onSaved(normalizeDocument(result.document), result.revision);
      props.onNotice(`规则 ${detailName} 已删除并热加载`);
      setPendingAction(null);
      setDetailName(null);
      clearDraft();
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "删除规则失败");
      setPendingAction(null);
    } finally {
      setSaving(false);
    }
  }

  async function toggleRule(rule: Rule) {
    if (togglingRuleName !== null) return;
    const enabled = rule.enabled === false;
    setTogglingRuleName(rule.name);
    props.onError("");
    try {
      const result = await api<{ revision: string; document: ConfigDocument }>(
        `/api/config/rules/${encodeURIComponent(rule.name)}`,
        {
          method: "PUT",
          body: JSON.stringify({
            revision: props.revision,
            name: rule.name,
            rule: { ...rule, enabled },
          }),
        },
      );
      props.onSaved(normalizeDocument(result.document), result.revision);
      props.onNotice(`规则 ${rule.name} 已${enabled ? "启用" : "停用"}并热加载`);
    } catch (reason) {
      props.onError(reason instanceof Error ? reason.message : "更新规则启用状态失败");
    } finally {
      setTogglingRuleName(null);
    }
  }

  const confirmation = useMemo<AgentActionConfirmation | null>(() => {
    if (!pendingAction) return null;
    if (pendingAction.kind === "discard") {
      return {
        eyebrow: "TRIGGER RULE",
        title: "放弃未保存的规则修改？",
        description: "当前详情中的修改尚未保存，继续后无法从页面恢复。",
        details: [
          { label: "规则", value: draftName || detailName || "新规则", mono: true },
          { label: "配置版本", value: shortRevision(props.revision), mono: true },
        ],
        impactTitle: "只放弃当前草稿",
        impact: "已保存配置、历史事件和正在运行的 Agent 不会受到影响。",
        confirmLabel: "放弃修改",
        dangerous: true,
      };
    }
    if (!detailName || !originalRule) return null;
    return {
      eyebrow: "TRIGGER RULE",
      title: `删除规则 ${detailName}？`,
      description: "删除后会立即保存配置并通知后台热加载。",
      details: [
        { label: "规则", value: detailName, mono: true },
        { label: "触发事件", value: `${originalRule.events.length} 个` },
        { label: "Agent", value: `${originalRule.agents.length} 个` },
      ],
      impactTitle: "停止后续匹配",
      impact: "后续事件不再匹配这条规则；已经生成的事件和历史 Agent 运行记录不会删除。",
      confirmLabel: "确认删除",
      dangerous: true,
    };
  }, [detailName, draftName, originalRule, pendingAction, props.revision]);

  function confirmPendingAction() {
    if (pendingAction?.kind === "delete") {
      void deleteRule();
      return;
    }
    if (pendingAction?.kind === "discard") {
      const nextName = pendingAction.nextName;
      setPendingAction(null);
      if (nextName) openDetail(nextName);
      else {
        clearDraft();
        setDetailName(null);
      }
    }
  }

  if (!creating && detailName === null) {
    return (
      <section className="section-card rule-list-page">
        <div className="section-title-row agent-list-title">
          <div>
            <h2>MR / PR 触发规则</h2>
            <p>可直接切换启用状态；点击一行查看完整配置并独立编辑和保存。</p>
          </div>
          <button type="button" className="button primary" onClick={beginCreate}>+ 添加规则</button>
        </div>
        <div className="agent-list-toolbar">
          <label>
            <span>搜索规则</span>
            <input value={query} placeholder="输入规则、事件、Agent 或仓库…" onChange={(event) => setQuery(event.target.value)} />
          </label>
          <span>共 {rules.length} 条规则</span>
        </div>
        <div className="rule-config-table">
          <div className="rule-config-table-head" aria-hidden="true">
            <span>规则</span><span>状态</span><span>触发事件</span><span>Agent</span><span>仓库范围</span><span>匹配选项</span><span />
          </div>
          <div className="rule-config-items">
            {visibleRules.map((rule) => {
              const conditionCount = Object.keys(rule.conditions ?? {}).length;
              const optionLabels = [
                rule.deduplicate_per_scan ? "单轮去重" : "逐事件触发",
                rule.inherit_workspace ? "继承工作区" : "独立工作区",
              ];
              const enabled = rule.enabled !== false;
              const toggling = togglingRuleName === rule.name;
              return (
                <div className="rule-config-row" key={rule.name} onClick={() => requestDetail(rule.name)}>
                  <span className="agent-config-identity"><span className="rule-config-avatar" aria-hidden="true">R</span><span><strong>{rule.name}</strong><small>{conditionCount > 0 ? `${conditionCount} 项条件` : "无附加条件"}</small></span></span>
                  <span className={`rule-config-status ${enabled ? "enabled" : ""}`} onClick={(event) => event.stopPropagation()}>
                    <Toggle
                      label={toggling ? "保存中…" : enabled ? "已启用" : "已停用"}
                      checked={enabled}
                      disabled={togglingRuleName !== null}
                      onChange={() => { void toggleRule(rule); }}
                    />
                  </span>
                  <span className="agent-config-summary"><strong>{rule.events.length} 个</strong><small>{rule.events[0] ?? "未选择事件"}</small></span>
                  <span className="agent-config-summary"><strong>{rule.agents.length} 个</strong><small>{rule.agents.join("、") || "未选择 Agent"}</small></span>
                  <span className="agent-config-summary"><strong>{rule.repositories?.length ? `${rule.repositories.length} 个` : "全部仓库"}</strong><small>{rule.repositories?.join("、") || "不限制仓库"}</small></span>
                  <span className="agent-config-summary"><strong>{rule.run_preflight ? "仓库 CI" : "跳过 CI"}</strong><small>{optionLabels.join(" · ")}</small></span>
                  <button
                    type="button"
                    className="agent-config-arrow rule-config-detail-button"
                    aria-label={`查看规则 ${rule.name}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      requestDetail(rule.name);
                    }}
                  >›</button>
                </div>
              );
            })}
            {visibleRules.length === 0 && <div className="empty tall">{rules.length === 0 ? "尚未配置触发规则" : "没有匹配的触发规则"}</div>}
          </div>
        </div>
      </section>
    );
  }

  const activeDocument = editing && draftDocument ? draftDocument : props.document;
  const activeName = editing ? draftName : detailName ?? "";
  return (
    <div className="agent-detail-page rule-detail-page">
      <header className="agent-detail-header">
        <button type="button" className="agent-detail-back" onClick={() => requestDetail(null)}><span aria-hidden="true">←</span>返回规则列表</button>
        <div className="agent-detail-heading">
          <div>
            <span className="eyebrow">TRIGGER RULE</span>
            <h2>{activeName}</h2>
            <p>配置版本 {shortRevision(props.revision)} · {creating ? "尚未保存的新规则" : editing ? "当前修改仅保存在页面草稿" : "当前已保存配置"}</p>
          </div>
          <span className={`agent-detail-mode ${editing ? "editing" : ""}`}>{creating ? "新建" : editing ? "编辑中" : "只读"}</span>
        </div>
        <div className="button-group agent-detail-actions">
          {editing ? (
            <>
              <button type="button" className="button secondary" disabled={saving} onClick={cancelEdit}>取消</button>
              <button type="button" className="button primary" disabled={!dirty || saving} onClick={() => { void saveRule(); }}>{saving ? "保存中…" : "保存规则"}</button>
            </>
          ) : (
            <>
              <button type="button" className="button danger" onClick={() => setPendingAction({ kind: "delete" })}>删除规则</button>
              <button type="button" className="button primary" onClick={beginEdit}>编辑规则</button>
            </>
          )}
        </div>
      </header>
      <fieldset className="config-editor-surface agent-detail-surface" disabled={!editing || saving}>
        <RulesEditor
          document={activeDocument}
          events={props.events}
          visibleIndexes={[detailIndex]}
          showOverview={false}
          allowDelete={false}
          onRename={setDraftName}
          onChange={setDraftDocument}
        />
      </fieldset>
      <AgentActionConfirmationDialog
        model={confirmation}
        busy={saving}
        onCancel={() => { if (!saving) setPendingAction(null); }}
        onConfirm={confirmPendingAction}
      />
    </div>
  );
}

function StatusPill({ value }: { value: string }) {
  return <span className={`status-pill status-${value}`}>{statusLabel(value)}</span>;
}

function queueReasonLabel(reason?: string | null): string | null {
  if (!reason) return null;
  const labels: Record<string, string> = {
    global_concurrency: "等待全局并发槽",
    runtime_concurrency: "等待运行时 Agent 并发槽",
    agent_concurrency: "等待此 Agent 并发槽",
    change_request_order: "等待同一 PR / MR 前序批次",
    event_retry_backoff: "等待前序事件重试",
    resource_lock: "等待变更请求或分支资源锁",
    repository_lock: "等待基础仓库管理锁",
  };
  return labels[reason] ?? reason;
}

function eventStatusPresentation(event: EventRecord): {
  label: string;
  visualStatus: string;
  details?: string;
} {
  const labels: Record<string, string> = {
    pending: "待处理",
    processing: "规则匹配中",
    unmatched: "未触发",
    triggered: "已触发",
    completed: event.trigger_count > 0 ? "已处理" : "已结束",
    failed: "处理失败",
    cancelled: "已取消",
  };
  let label = labels[event.status] ?? event.status;
  let visualStatus = event.status;
  if (event.error?.includes("状态回写失败")) {
    label = "状态回写失败";
    visualStatus = "failed";
  } else if (event.status === "processing" && event.preflight_status === "running") {
    label = "本地 CI 中";
  } else if (event.status === "completed" && event.preflight_status === "failure") {
    label = "本地 CI 未通过";
    visualStatus = "failed";
  } else if (event.status === "completed" && event.preflight_status === "timed_out") {
    label = "本地 CI 超时";
    visualStatus = "failed";
  } else if (event.status === "completed" && event.preflight_status === "superseded") {
    label = "Head 已更新，已跳过";
    visualStatus = "unmatched";
  } else if (event.status === "failed" && event.preflight_status === "error") {
    label = "本地 CI 异常";
  }
  const details = event.error
    ?? event.preflight_error
    ?? (event.preflight_failed_step ? `失败步骤：${event.preflight_failed_step}` : undefined);
  return { label, visualStatus, details };
}

function EventStatusPill({
  event,
  onClick,
}: {
  event: EventRecord;
  onClick?: () => void;
}) {
  const presentation = eventStatusPresentation(event);
  const content = (
    <span
      className={`status-pill status-${presentation.visualStatus}`}
      title={presentation.details}
    >
      {presentation.label}
      {event.status === "pending" && queueReasonLabel(event.queue_reason)
        ? ` · ${queueReasonLabel(event.queue_reason)}`
        : ""}
    </span>
  );
  if (!onClick) return content;
  return (
    <button
      type="button"
      className="event-status-trigger"
      aria-label={`查看事件状态详情：${presentation.label}`}
      title="查看事件状态详情"
      onClick={onClick}
    >
      {content}
    </button>
  );
}

function EventAgentProgress({ event }: { event: EventRecord }) {
  const rootCount = event.trigger_count;
  const subAgentCount = event.sub_agent_count ?? 0;
  const total = rootCount + subAgentCount;
  if (total === 0) {
    return <span className="event-agent-none">—</span>;
  }
  const scope = `根 Agent ${rootCount} · sub-agent ${subAgentCount}`;
  const settled = event.agent_completed_count
    + event.agent_failed_count
    + event.agent_timed_out_count
    + event.agent_cancelled_count;
  if (event.agent_running_count > 0) {
    return (
      <span className="event-agent-progress running">
        {scope} · 执行中 {event.agent_running_count} · 已结束 {settled}/{total}
      </span>
    );
  }
  if (event.agent_preparing_count > 0) {
    return (
      <span className="event-agent-progress preparing">
        {scope} · 准备工作区 {event.agent_preparing_count} · 已结束 {settled}/{total}
      </span>
    );
  }
  if (event.agent_queued_count > 0) {
    return (
      <span className="event-agent-progress queued">
        {scope} · 排队中 {event.agent_queued_count} · 已结束 {settled}/{total}
      </span>
    );
  }
  const failed = event.agent_failed_count
    + event.agent_timed_out_count
    + event.agent_cancelled_count;
  if (failed > 0) {
    return (
      <span className="event-agent-progress failed">
        {scope} · 异常 {failed} · 已结束 {settled}/{total}
      </span>
    );
  }
  return (
    <span className="event-agent-progress completed">
      {scope} · 已完成 {event.agent_completed_count}/{total}
    </span>
  );
}

type EventAgentTreeRow = {
  key: string;
  agentName: string;
  description: string;
  runId?: string | null;
  status: string;
  depth: number;
};

function buildEventAgentTreeRows(
  dispatches: EventDispatchDetail[],
  agentRuns: EventAgentRunSummary[],
): EventAgentTreeRow[] {
  const rows: EventAgentTreeRow[] = [];
  const childrenByParent = new Map<string, EventAgentRunSummary[]>();
  for (const run of agentRuns) {
    if (!run.parent_run_id) continue;
    const children = childrenByParent.get(run.parent_run_id) ?? [];
    children.push(run);
    childrenByParent.set(run.parent_run_id, children);
  }

  const visited = new Set<string>();
  const appendChildren = (parentRunId: string, parentAgentName: string, depth: number) => {
    for (const child of childrenByParent.get(parentRunId) ?? []) {
      if (visited.has(child.run_id)) continue;
      visited.add(child.run_id);
      rows.push({
        key: child.run_id,
        agentName: child.agent_name,
        description: `sub-agent · 由 ${parentAgentName} 触发`,
        runId: child.run_id,
        status: child.run_status,
        depth: depth + 1,
      });
      appendChildren(child.run_id, child.agent_name, depth + 1);
    }
  };

  for (const dispatch of dispatches) {
    if (dispatch.run_id) visited.add(dispatch.run_id);
    rows.push({
      key: `${dispatch.idempotency_key}:${dispatch.agent_name}`,
      agentName: dispatch.agent_name,
      description: `根 Agent · 规则 ${dispatch.rule_name}`,
      runId: dispatch.run_id,
      status: dispatch.run_status ?? "queued",
      depth: 0,
    });
    if (dispatch.run_id) appendChildren(dispatch.run_id, dispatch.agent_name, 0);
  }

  // 历史异常数据若缺失父节点，也应保留可追溯入口，不能静默隐藏运行记录。
  for (const run of agentRuns) {
    if (visited.has(run.run_id)) continue;
    rows.push({
      key: run.run_id,
      agentName: run.agent_name,
      description: run.parent_run_id ? "sub-agent · 父运行未找到" : "根 Agent",
      runId: run.run_id,
      status: run.run_status,
      depth: run.parent_run_id ? 1 : 0,
    });
  }
  return rows;
}

function preflightStatusLabel(status?: string | null): string {
  const labels: Record<string, string> = {
    running: "执行中",
    success: "通过",
    failure: "未通过",
    timed_out: "超时",
    error: "执行异常",
    cancelled: "已取消",
    superseded: "Head 已更新，已跳过",
  };
  return status ? labels[status] ?? status : "未执行";
}

function preflightStatusClass(status: string): string {
  if (status === "success") return "completed";
  if (status === "running") return "processing";
  if (status === "superseded") return "unmatched";
  if (status === "cancelled") return "cancelled";
  return "failed";
}

function preflightPhaseLabel(phase?: string | null): string {
  const labels: Record<string, string> = {
    queued: "等待启动",
    waiting_lock: "等待仓库锁",
    preparing: "准备代码工作区",
    preparing_cache: "准备依赖缓存",
    running_steps: "执行 CI 步骤",
    cancelling: "正在取消",
    finished: "执行结束",
  };
  return phase ? labels[phase] ?? phase : "等待状态";
}

function preflightStepStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    pending: "等待执行",
    running: "执行中",
    success: "通过",
    failure: "未通过",
    timed_out: "超时",
    error: "执行异常",
    cancelled: "已取消",
    skipped: "已跳过",
  };
  return labels[status] ?? status;
}

function preflightStepStatusClass(status: string): string {
  if (status === "success") return "completed";
  if (status === "running") return "processing";
  if (status === "pending") return "queued";
  if (status === "skipped") return "unmatched";
  if (status === "cancelled") return "cancelled";
  return "failed";
}

function eventDetailNeedsRefresh(detail: EventDetailRecord): boolean {
  if (["pending", "processing"].includes(detail.status)) return true;
  if (detail.preflights.some((preflight) => preflight.status === "running")) return true;
  if (detail.agent_queued_count + detail.agent_preparing_count + detail.agent_running_count > 0) return true;
  if (detail.dispatches.some((dispatch) => ["queued", "preparing", "running"].includes(dispatch.run_status ?? ""))) return true;
  return (detail.agent_runs ?? []).some((run) => ["queued", "preparing", "running"].includes(run.run_status));
}

function eventStatusExplanation(event: EventRecord): string {
  if (event.error?.includes("状态回写失败")) {
    return "事件处理已结束，但向平台回写状态时失败。";
  }
  if (event.preflight_status === "running") {
    return "本地 Preflight / CI 正在执行，规则将在检查结束后继续处理。";
  }
  if (event.preflight_status === "superseded") {
    return "事件记录的 Head 已被后续提交取代且无法再获取，本次检查已跳过；同一 MR / PR 的后续事件会继续处理。";
  }
  if (["failure", "timed_out", "error"].includes(event.preflight_status ?? "")) {
    return event.preflight_reused
      ? "本事件复用了相同提交与配置的历史 Preflight / CI 结果，因此没有重复运行 Agent。"
      : "本地 Preflight / CI 未通过，因此没有继续启动 Agent。";
  }
  if (event.status === "pending") {
    return queueReasonLabel(event.queue_reason) ?? "事件正在等待调度。";
  }
  if (event.status === "processing") return "正在匹配触发规则并准备后续处理。";
  if (event.status === "unmatched") return "没有启用的触发规则匹配这个事件。";
  if (event.status === "triggered") return "规则已匹配，Agent 正在排队或运行。";
  if (event.status === "failed") return "事件处理发生异常，详情见错误信息。";
  if (event.status === "cancelled") return "事件关联的运行已被取消。";
  if (event.trigger_count > 0) return "事件已完成规则匹配，关联的 Agent 调度已经结束。";
  return "事件处理流程已结束，本次没有产生 Agent 调度或本地 CI 记录。";
}

function drawerLayerStyle(depth: number): { zIndex: number; "--drawer-width": string } {
  const width = Math.max(44, 60 - depth * 4);
  return {
    zIndex: 80 + depth * 2,
    "--drawer-width": `${width}vw`,
  };
}

function ChangeRequestDetailDrawer(props: {
  changeRequest: ChangeRequestRecord | null;
  active: boolean;
  depth: number;
  onOpenEvent: (event: EventRecord) => void;
  onClose: () => void;
}) {
  const { changeRequest, active, depth, onClose, onOpenEvent } = props;
  const [detail, setDetail] = useState<ChangeRequestDetailRecord | null>(null);
  const [error, setError] = useState("");

  useBodyScrollLock(changeRequest !== null);

  useEffect(() => {
    if (!changeRequest) {
      setDetail(null);
      setError("");
      return undefined;
    }
    let disposed = false;
    const parameters = new URLSearchParams({
      repository_id: changeRequest.repository_id,
      number: String(changeRequest.number),
    });
    void api<ChangeRequestDetailRecord>(`/api/change-request-detail?${parameters.toString()}`)
      .then((next) => {
        if (!disposed) {
          setDetail(next);
          setError("");
        }
      })
      .catch((reason) => {
        if (!disposed) setError(reason instanceof Error ? reason.message : "MR/PR 详情加载失败");
      });
    return () => { disposed = true; };
  }, [changeRequest]);

  useEffect(() => {
    if (!changeRequest || !active) return undefined;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [active, changeRequest, onClose]);

  if (!changeRequest) return null;
  const current = detail ?? changeRequest;
  return (
    <div className="run-drawer-layer" style={drawerLayerStyle(depth)} aria-hidden={!active}>
      <button type="button" className="run-drawer-backdrop" aria-label="关闭 MR/PR 详情" disabled={!active} onClick={onClose} />
      <aside className="run-drawer event-detail-drawer" role="dialog" aria-modal={active} aria-label="MR/PR 详情">
        <header className="run-drawer-head">
          <div>
            <span className="eyebrow">{current.repository_id} · #{current.number}</span>
            <h2>{current.title}</h2>
            <p>{current.source_branch} → {current.target_branch}</p>
          </div>
          <div className="run-drawer-actions">
            <StatusPill value={current.state} />
            <button className="run-drawer-close" aria-label="关闭" disabled={!active} onClick={onClose}>×</button>
          </div>
        </header>
        <div className="run-drawer-body event-detail-body">
          {error && <div className="alert error">{error}</div>}
          {!detail && !error && <div className="empty tall">正在加载 MR/PR 详情…</div>}
          {detail && (
            <>
              <section className="event-detail-section">
                <div className="event-detail-section-title"><div><span className="eyebrow">CHANGE REQUEST</span><h3>当前快照</h3></div></div>
                <dl className="run-metadata">
                  <div><dt>仓库</dt><dd>{detail.repository_id}</dd></div>
                  <div><dt>编号</dt><dd>#{detail.number}</dd></div>
                  <div><dt>Head SHA</dt><dd>{detail.head_sha}</dd></div>
                  <div><dt>远端更新</dt><dd>{dateTimeText(detail.updated_at)}</dd></div>
                  <div><dt>最近扫描</dt><dd>{timeText(detail.scanned_at)}</dd></div>
                  <div><dt>平台地址</dt><dd><a href={detail.web_url} target="_blank" rel="noreferrer">打开 MR / PR</a></dd></div>
                </dl>
              </section>
              <section className="event-detail-section">
                <div className="event-detail-section-title">
                  <div><span className="eyebrow">EVENTS</span><h3>关联事件</h3></div>
                  <span className="event-detail-count">{detail.events.length}</span>
                </div>
                {detail.events.length === 0 ? (
                  <div className="event-detail-empty">当前 MR/PR 尚无关联事件。</div>
                ) : (
                  <div className="event-dispatch-list">
                    {detail.events.map((event) => (
                      <button type="button" className="event-record-card" key={event.event_id} onClick={() => onOpenEvent(event)}>
                        <span><strong>{event.event_type}</strong><small>{dateTimeText(event.occurred_at)}</small></span>
                        <EventStatusPill event={event} />
                        <span className="run-row-arrow" aria-hidden="true">›</span>
                      </button>
                    ))}
                  </div>
                )}
              </section>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

function EventDetailDrawer(props: {
  event: EventRecord | null;
  active: boolean;
  depth: number;
  onOpenAgent: (runId: string) => void;
  onOpenPreflight: (runId: string) => void;
  onClose: () => void;
}) {
  const { event, active, depth, onClose, onOpenAgent, onOpenPreflight } = props;
  const [detail, setDetail] = useState<EventDetailRecord | null>(null);
  const [error, setError] = useState("");

  useBodyScrollLock(event !== null);

  useEffect(() => {
    const eventId = event?.event_id;
    if (!eventId) {
      setDetail(null);
      setError("");
      return undefined;
    }
    let disposed = false;
    let timer: number | undefined;
    const load = async (): Promise<void> => {
      let refreshAgain = true;
      try {
        const next = await api<EventDetailRecord>(`/api/events/${encodeURIComponent(eventId)}`);
        if (!disposed) {
          setDetail(next);
          setError("");
          refreshAgain = eventDetailNeedsRefresh(next);
        }
      } catch (reason) {
        if (!disposed) setError(reason instanceof Error ? reason.message : "事件详情加载失败");
      }
      if (!disposed && refreshAgain) {
        timer = window.setTimeout(() => { void load(); }, 3000);
      }
    };
    setDetail(null);
    setError("");
    void load();
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [event?.event_id]);

  useEffect(() => {
    if (!event || !active) return undefined;
    const closeOnEscape = (keyboardEvent: KeyboardEvent) => {
      if (keyboardEvent.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [active, event, onClose]);

  if (!event) return null;
  const current = detail ?? event;
  const preflights = detail?.preflights ?? (detail?.preflight ? [detail.preflight] : []);
  const agentRows = detail
    ? buildEventAgentTreeRows(detail.dispatches, detail.agent_runs ?? [])
    : [];
  return (
    <div className="run-drawer-layer" style={drawerLayerStyle(depth)} aria-hidden={!active}>
      <button type="button" className="run-drawer-backdrop" aria-label="关闭事件详情" disabled={!active} onClick={onClose} />
      <aside className="run-drawer event-detail-drawer" role="dialog" aria-modal={active} aria-label="事件状态详情">
        <header className="run-drawer-head">
          <div>
            <span className="eyebrow">{current.event_id}</span>
            <h2>{current.event_type}</h2>
            <p>{current.repository_id} · #{current.number} · {dateTimeText(current.occurred_at)}</p>
          </div>
          <div className="run-drawer-actions">
            <EventStatusPill event={current} />
            <button className="run-drawer-close" aria-label="关闭" disabled={!active} onClick={onClose}>×</button>
          </div>
        </header>
        <div className="run-drawer-body event-detail-body">
          {error && <div className="alert error">{error}</div>}
          {!detail && !error && <div className="empty tall">正在加载事件详情…</div>}
          {detail && (
            <>
              <section className="event-detail-section">
                <div className="event-detail-section-title">
                  <div><span className="eyebrow">EVENT</span><h3>处理结论</h3></div>
                  <EventStatusPill event={detail} />
                </div>
                <p className="event-detail-explanation">{eventStatusExplanation(detail)}</p>
                <dl className="run-metadata">
                  <div><dt>来源</dt><dd>{detail.origin === "manual" ? "手动触发" : "扫描器"}</dd></div>
                  <div><dt>事件状态</dt><dd>{detail.status}</dd></div>
                  <div><dt>尝试次数</dt><dd>{detail.attempts}</dd></div>
                  <div><dt>排队原因</dt><dd>{queueReasonLabel(detail.queue_reason) ?? "—"}</dd></div>
                  <div><dt>平台活动</dt><dd>{detail.source_activity_type ?? "—"}</dd></div>
                  <div><dt>平台活动时间</dt><dd>{dateTimeText(detail.source_occurred_at)}</dd></div>
                </dl>
                {detail.error && <pre className="detail-pre detail-error">{detail.error}</pre>}
              </section>
              <section className="event-detail-section">
                <div className="event-detail-section-title">
                  <div><span className="eyebrow">PREFLIGHT / CI</span><h3>本地检查记录</h3></div>
                  <span className="event-detail-count">{preflights.length}</span>
                </div>
                {preflights.length === 0 ? (
                  <div className="event-detail-empty">本事件没有关联的 Preflight / CI 记录。</div>
                ) : (
                  <div className="event-dispatch-list">
                    {preflights.map((preflight) => (
                      <button type="button" className="event-record-card" key={preflight.run_id} onClick={() => onOpenPreflight(preflight.run_id)}>
                        <span>
                          <strong>{preflight.reused ? "复用历史结果" : "本批次新执行"}</strong>
                          <small>{preflight.failed_step ? `失败步骤：${preflight.failed_step}` : `开始于 ${timeText(preflight.started_at)}`}</small>
                        </span>
                        <span className={`status-pill status-${preflightStatusClass(preflight.status)}`}>{preflightStatusLabel(preflight.status)}</span>
                        <span className="run-row-arrow" aria-hidden="true">›</span>
                      </button>
                    ))}
                  </div>
                )}
              </section>
              <section className="event-detail-section">
                <div className="event-detail-section-title">
                  <div><span className="eyebrow">AGENT</span><h3>规则与运行</h3></div>
                  <span className="event-detail-count">{agentRows.length}</span>
                </div>
                {agentRows.length === 0 ? (
                  <div className="event-detail-empty">本事件没有产生 Agent 调度。</div>
                ) : (
                  <div className="event-dispatch-list">
                    {agentRows.map((run) => {
                      const indent = Math.min(run.depth, 6) * 18;
                      return (
                      <button
                        type="button"
                        className={`event-record-card${run.depth > 0 ? " event-record-card-sub-agent" : ""}`}
                        key={run.key}
                        disabled={!run.runId}
                        style={{ marginLeft: indent, width: `calc(100% - ${indent}px)` }}
                        onClick={() => { if (run.runId) onOpenAgent(run.runId); }}
                      >
                        <span><strong>{run.depth > 0 ? "↳ " : ""}{run.agentName}</strong><small>{run.description}</small></span>
                        <StatusPill value={run.status} />
                        <span className="run-row-arrow" aria-hidden="true">›</span>
                      </button>
                      );
                    })}
                  </div>
                )}
              </section>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

function PreflightRunDetailDrawer(props: {
  runId: string;
  depth: number;
  onClose: () => void;
}) {
  const { runId, depth, onClose } = props;
  const [detail, setDetail] = useState<PreflightRunDetail | null>(null);
  const [error, setError] = useState("");
  const [logs, setLogs] = useState<RunLog[]>([]);
  const [streamError, setStreamError] = useState("");
  const [cancelling, setCancelling] = useState(false);
  const liveOutputRef = useRef<HTMLPreElement | null>(null);

  useBodyScrollLock(Boolean(runId));

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    const load = async (): Promise<void> => {
      let refreshAgain = true;
      try {
        const next = await api<PreflightRunDetail>(`/api/preflight-runs/${encodeURIComponent(runId)}`);
        if (!disposed) {
          setDetail(next);
          setError("");
          refreshAgain = next.status === "running";
        }
      } catch (reason) {
        if (!disposed) setError(reason instanceof Error ? reason.message : "本地 CI 详情加载失败");
      }
      if (!disposed && refreshAgain) {
        timer = window.setTimeout(() => { void load(); }, 1000);
      }
    };
    setDetail(null);
    setError("");
    void load();
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runId]);

  useEffect(() => {
    const controller = new AbortController();
    let cursor = 0;
    setLogs([]);
    setStreamError("");
    void streamPreflightLogs(
      runId,
      cursor,
      controller.signal,
      (log) => {
        cursor = Math.max(cursor, log.id);
        setLogs((current) => (
          current.some((item) => item.id === log.id) ? current : [...current, log]
        ));
      },
    ).catch((reason) => {
      if (!controller.signal.aborted) {
        setStreamError(reason instanceof Error ? reason.message : "CI 实时日志连接中断");
      }
    });
    return () => controller.abort();
  }, [runId]);

  useEffect(() => {
    const output = liveOutputRef.current;
    if (output) output.scrollTop = output.scrollHeight;
  }, [logs]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  async function cancelManualPreflight() {
    if (!detail || detail.trigger_source !== "manual" || detail.status !== "running") return;
    setCancelling(true);
    setError("");
    try {
      const result = await api<{ accepted: boolean; reason: string }>(
        `/api/preflight-runs/${encodeURIComponent(runId)}/cancel`,
        { method: "POST" },
      );
      if (!result.accepted) setError(result.reason);
      setDetail((current) => current ? { ...current, cancel_requested: true, phase: "cancelling" } : current);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消手动 CI 失败");
    } finally {
      setCancelling(false);
    }
  }

  const liveOutput = logs.map((log) => {
    if (log.event_type === "git_progress") {
      try {
        const progress = JSON.parse(log.payload) as {
          operation?: string;
          state?: string;
          elapsed_seconds?: number;
        };
        return `[Git] ${progress.operation ?? "Git 操作"} · ${progress.state ?? "运行中"} · ${progress.elapsed_seconds ?? 0} 秒\n`;
      } catch {
        return `${log.payload}\n`;
      }
    }
    if (log.event_type === "completed") {
      try {
        const result = JSON.parse(log.payload) as { status?: string };
        return `\n[完成] ${preflightStatusLabel(result.status)}\n`;
      } catch {
        return `\n[完成] ${log.payload}\n`;
      }
    }
    return log.payload;
  }).join("");
  const activeStep = detail?.steps.find((step) => step.status === "running");
  const progressLabel = activeStep
    ? `步骤 ${activeStep.step_index + 1}：${activeStep.name}`
    : preflightPhaseLabel(detail?.phase);

  return (
    <div className="run-drawer-layer" style={drawerLayerStyle(depth)}>
      <button type="button" className="run-drawer-backdrop" aria-label="关闭本地 CI 详情" onClick={onClose} />
      <aside className="run-drawer event-detail-drawer" role="dialog" aria-modal="true" aria-label="本地 CI 运行详情">
        <header className="run-drawer-head">
          <div>
            <span className="eyebrow">{runId}</span>
            <h2>本地 Preflight / CI</h2>
            {detail && <p>{detail.repository_id} · {detail.number ? `#${detail.number}` : detail.branch ?? "默认分支"} · {detail.trigger_source === "manual" ? "手动执行" : detail.event_type ?? "事件检查"}</p>}
          </div>
          <div className="run-drawer-actions">
            {detail && <span className={`status-pill status-${preflightStatusClass(detail.status)}`}>{preflightStatusLabel(detail.status)}</span>}
            {detail?.trigger_source === "manual" && detail.status === "running" && (
              <button
                type="button"
                className="button danger compact"
                disabled={cancelling || Boolean(detail.cancel_requested)}
                onClick={() => { void cancelManualPreflight(); }}
              >{detail.cancel_requested || cancelling ? "取消中…" : "取消 CI"}</button>
            )}
            <button className="run-drawer-close" aria-label="关闭" onClick={onClose}>×</button>
          </div>
        </header>
        <div className="run-drawer-body event-detail-body">
          {error && <div className="alert error">{error}</div>}
          {!detail && !error && <div className="empty tall">正在加载本地 CI 详情…</div>}
          {detail && (
            <>
              <section className="event-detail-section preflight-live-section">
                <div className="event-detail-section-title">
                  <div><span className="eyebrow">LIVE</span><h3>实时执行过程</h3></div>
                  <span className="event-detail-count">{detail.status === "running" ? progressLabel : preflightStatusLabel(detail.status)}</span>
                </div>
                {streamError && <div className="alert error">{streamError}；步骤状态仍会继续刷新。</div>}
                <pre ref={liveOutputRef} className="detail-pre preflight-live-output">{liveOutput || (detail.status === "running" ? "正在等待 CI 输出…" : detail.output || "该历史记录没有实时日志")}</pre>
              </section>
              <section className="event-detail-section">
                <div className="event-detail-section-title"><div><span className="eyebrow">RESULT</span><h3>执行结论</h3></div></div>
                <dl className="run-metadata">
                  <div><dt>失败步骤</dt><dd>{detail.failed_step ?? "—"}</dd></div>
                  <div><dt>退出码</dt><dd>{detail.exit_code ?? "—"}</dd></div>
                  <div><dt>触发来源</dt><dd>{detail.trigger_source === "manual" ? "仓库手动执行" : "MR / PR 事件"}</dd></div>
                  <div><dt>当前阶段</dt><dd>{progressLabel}</dd></div>
                  <div><dt>分支</dt><dd>{detail.branch ?? "—"}</dd></div>
                  <div><dt>Head SHA</dt><dd>{detail.head_sha}</dd></div>
                  <div><dt>依赖缓存</dt><dd>{detail.cache_path ?? "未启用"}</dd></div>
                  <div><dt>配置版本</dt><dd>{detail.config_revision}</dd></div>
                  <div><dt>执行次数</dt><dd>{detail.attempts}</dd></div>
                  <div><dt>开始时间</dt><dd>{timeText(detail.started_at)}</dd></div>
                  <div><dt>结束时间</dt><dd>{timeText(detail.finished_at)}</dd></div>
                  <div><dt>耗时</dt><dd>{durationText(detail.started_at, detail.finished_at)}</dd></div>
                  <div><dt>平台状态回写</dt><dd>{detail.trigger_source === "manual" ? "手动 CI 不回写" : detail.status === "superseded" ? "Head 已更新，无需回写" : detail.status_published ? "成功" : "未完成"}</dd></div>
                </dl>
                {detail.error && <><h4>错误信息</h4><pre className="detail-pre detail-error">{detail.error}</pre></>}
                <h4>命令步骤</h4>
                {(detail.steps ?? []).length === 0 ? (
                  <div className="event-detail-empty">该记录没有步骤级快照，可能由旧版本服务创建。</div>
                ) : (
                  <div className="preflight-step-run-list">
                    {detail.steps.map((step) => (
                      <article className="preflight-step-run-card" key={`${step.step_index}:${step.name}`}>
                        <header>
                          <span><strong>{step.step_index + 1}. {step.name}</strong><small>参数数组按本次运行配置固化</small></span>
                          <span className={`status-pill status-${preflightStepStatusClass(step.status)}`}>{preflightStepStatusLabel(step.status)}</span>
                        </header>
                        <pre className="preflight-step-command">{JSON.stringify(step.command, null, 2)}</pre>
                        <dl>
                          <div><dt>超时</dt><dd>{step.timeout_seconds === null || step.timeout_seconds === undefined ? "—" : `${step.timeout_seconds} 秒`}</dd></div>
                          <div><dt>耗时</dt><dd>{step.started_at ? durationText(step.started_at, step.finished_at) : "—"}</dd></div>
                          <div><dt>退出码</dt><dd>{step.exit_code ?? "—"}</dd></div>
                          <div><dt>开始时间</dt><dd>{timeText(step.started_at)}</dd></div>
                        </dl>
                        {step.error && <pre className="preflight-step-error">{step.error}</pre>}
                      </article>
                    ))}
                  </div>
                )}
                <h4>检查输出</h4>
                <pre className={`detail-pre ${["failure", "timed_out", "error"].includes(detail.status) ? "detail-error" : ""}`}>{detail.status === "running" ? "运行中，完整有界输出将在结束后固化；请查看上方实时执行过程。" : detail.output || "暂无输出"}</pre>
              </section>
              <section className="event-detail-section">
                <div className="event-detail-section-title">
                  <div><span className="eyebrow">EVENTS</span><h3>关联事件</h3></div>
                  <span className="event-detail-count">{detail.linked_events.length}</span>
                </div>
                <div className="event-linked-list">
                  {detail.linked_events.length === 0 && (
                    <div className="event-detail-empty">
                      {detail.trigger_source === "manual"
                        ? "手动 CI 不绑定 MR / PR 事件，也不会触发 Agent。"
                        : "当前 CI 记录没有可读取的关联事件。"}
                    </div>
                  )}
                  {detail.linked_events.map((event) => (
                    <div key={event.event_id}><strong>{event.event_type}</strong><span>{event.reused ? "复用历史结果" : "本批次新执行"} · {dateTimeText(event.occurred_at)}</span></div>
                  ))}
                </div>
              </section>
            </>
          )}
        </div>
      </aside>
    </div>
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

function modelSettingSourceLabel(source?: string | null): string {
  const labels: Record<string, string> = {
    agent: "Agent 覆盖",
    runtime: "运行时默认",
    codex_user: "Codex 用户配置",
    codex_default: "Codex / 账号默认",
  };
  return source ? labels[source] ?? source : "来源未记录";
}

function modelSettingText(
  value: string | null | undefined,
  source: string | null | undefined,
  fallback = "模型默认",
): string {
  return `${value || fallback} · ${modelSettingSourceLabel(source)}`;
}

function modelExecutionModeLabel(mode?: string | null): string {
  if (mode === "model") return "模型基座";
  if (mode === "cli") return "Codex CLI";
  return "历史运行未记录";
}

function modelFastModeLabel(mode?: string | null): string {
  if (mode === "fast") return "快速";
  if (mode === "standard") return "标准";
  return mode || "模型默认";
}

function modelVerbosityLabel(verbosity?: string | null): string {
  const labels: Record<string, string> = { low: "低", medium: "中", high: "高" };
  return verbosity ? labels[verbosity] ?? verbosity : "模型默认";
}

function runTargetText(run: RunSummary): string {
  if (run.repository_id && run.change_request_number !== undefined && run.change_request_number !== null) {
    return `${run.repository_id} · #${run.change_request_number}`;
  }
  return run.resource_key;
}

function AgentRunDetailDrawer(props: {
  initialRunId: string;
  depth?: number;
  onClose: () => void;
  onRefresh: () => void;
}) {
  const [selectedId, setSelectedId] = useState(props.initialRunId);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [logs, setLogs] = useState<RunLog[]>([]);
  const [error, setError] = useState("");
  const [cancelling, setCancelling] = useState(false);
  const [cancelConfirmationOpen, setCancelConfirmationOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState<RunDrawerTab>("messages");
  const [childRunId, setChildRunId] = useState<string | null>(null);
  const visibleMessageCount = useMemo(() => presentRunLogs(logs).length, [logs]);

  useBodyScrollLock(true);

  function openRun(runId: string) {
    setCancelConfirmationOpen(false);
    setChildRunId(runId);
  }

  function closeDrawer() {
    setCancelConfirmationOpen(false);
    setDetail(null);
    setLogs([]);
    setError("");
    setChildRunId(null);
    props.onClose();
  }

  useEffect(() => {
    setSelectedId(props.initialRunId);
    setDrawerTab("messages");
    setChildRunId(null);
  }, [props.initialRunId]);

  async function cancelSelectedRun() {
    if (!detail) return;
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
      setCancelConfirmationOpen(false);
      setCancelling(false);
    }
  }

  const cancelConfirmation = useMemo<AgentActionConfirmation | null>(() => {
    if (!cancelConfirmationOpen || !detail) return null;
    return {
      eyebrow: "RUN CANCELLATION",
      title: `取消 ${detail.agent_name} 的本次运行？`,
      description: "取消请求提交后，本次运行将进入终止流程。",
      details: [
        { label: "Agent", value: detail.agent_name },
        { label: "仓库 / MR / PR", value: runTargetText(detail) },
        { label: "运行 ID", value: selectedId, mono: true },
        { label: "当前状态", value: statusLabel(detail.status) },
      ],
      impactTitle: "将递归取消本次运行",
      impact: "本次运行的全部 sub-agent 也会收到取消请求；正在执行的 Codex 或 Git 进程组可能被终止。",
      confirmLabel: "确认取消运行",
      dangerous: true,
    };
  }, [cancelConfirmationOpen, detail, selectedId]);

  useEffect(() => {
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
    const closeOnEscape = (event: KeyboardEvent) => {
      if (
        event.key === "Escape"
        && childRunId === null
        && !cancelConfirmationOpen
        && !cancelling
      ) {
        closeDrawer();
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [cancelConfirmationOpen, cancelling, selectedId, childRunId]);

  return (
    <>
      <div
        className={`run-drawer-layer${childRunId ? " run-drawer-layer-inactive" : ""}`}
        style={drawerLayerStyle(props.depth ?? 0)}
        aria-hidden={childRunId !== null}
      >
          <button className="run-drawer-backdrop" aria-label="关闭运行详情" disabled={childRunId !== null} onClick={closeDrawer} />
          <aside className="run-drawer layered-detail-drawer" role="dialog" aria-modal="true" aria-label="Agent 运行详情">
            <header className="run-drawer-head">
              <div>
                <span className="eyebrow">{detail?.run_id ?? selectedId}</span>
                <h2>{detail?.agent_name ?? "正在加载…"}</h2>
                {detail && <p>{runTargetText(detail)} · {detail.rule_name ?? "Sub-agent 调用"}</p>}
              </div>
              <div className="run-drawer-actions">
                {detail && (["queued", "preparing", "running"].includes(detail.status)) && (
                  <button className="button danger" disabled={cancelling} onClick={() => setCancelConfirmationOpen(true)}>
                    {cancelling ? "取消中…" : "取消运行"}
                  </button>
                )}
                {detail && <StatusPill value={detail.status} />}
                <button className="run-drawer-close" aria-label="关闭" onClick={closeDrawer}>×</button>
              </div>
            </header>
            <nav className="run-drawer-tabs" aria-label="运行详情分类">
              <button className={drawerTab === "messages" ? "active" : ""} onClick={() => setDrawerTab("messages")}>消息 <span>{visibleMessageCount}</span></button>
              <button className={drawerTab === "result" ? "active" : ""} onClick={() => setDrawerTab("result")}>最终结果</button>
              <button className={drawerTab === "context" ? "active" : ""} onClick={() => setDrawerTab("context")}>运行详情</button>
            </nav>
            <div className="run-drawer-body">
              {error && <div className="alert error">{error}</div>}
              {!detail && !error && <div className="empty tall">正在加载运行详情…</div>}
              {detail && drawerTab === "messages" && (
                <RunMessageFeed
                  logs={logs}
                  active={childRunId === null}
                  onOpenRun={openRun}
                />
              )}
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
                      <div><dt>排队原因</dt><dd>{queueReasonLabel(detail.queue_reason) ?? "—"}</dd></div>
                      <div><dt>开始时间</dt><dd>{timeText(detail.started_at)}</dd></div>
                      <div><dt>结束时间</dt><dd>{timeText(detail.finished_at)}</dd></div>
                      <div><dt>模型会话</dt><dd>{detail.thread_id ?? "—"}</dd></div>
                      <div><dt>执行模式</dt><dd>{modelExecutionModeLabel(detail.model_snapshot?.execution_mode)}</dd></div>
                      <div><dt>Provider</dt><dd>{detail.model_snapshot
                        ? `${detail.model_snapshot.provider_name ?? detail.model_snapshot.provider_id ?? "Codex"}${detail.model_snapshot.provider_enabled === false ? "（已停用）" : ""}`
                        : "历史运行未记录"}</dd></div>
                      <div><dt>模型</dt><dd>{detail.model_snapshot
                        ? detail.model_snapshot.resolved_label ?? detail.model_snapshot.model ?? "Provider 默认模型（暂未解析）"
                        : "历史运行未记录"}</dd></div>
                      <div><dt>模型来源</dt><dd>{detail.model_snapshot
                        ? modelSettingSourceLabel(detail.model_snapshot.model_source)
                        : "—"}</dd></div>
                      <div><dt>推理强度</dt><dd>{detail.model_snapshot
                        ? modelSettingText(detail.model_snapshot.reasoning_effort, detail.model_snapshot.reasoning_effort_source)
                        : "—"}</dd></div>
                      <div><dt>快速模式</dt><dd>{detail.model_snapshot
                        ? modelSettingText(modelFastModeLabel(detail.model_snapshot.fast_mode), detail.model_snapshot.fast_mode_source)
                        : "—"}</dd></div>
                      <div><dt>输出详细度</dt><dd>{detail.model_snapshot
                        ? modelSettingText(modelVerbosityLabel(detail.model_snapshot.verbosity), detail.model_snapshot.verbosity_source)
                        : "—"}</dd></div>
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
      <AgentActionConfirmationDialog
        model={cancelConfirmation}
        busy={cancelling}
        onCancel={() => { if (!cancelling) setCancelConfirmationOpen(false); }}
        onConfirm={() => { void cancelSelectedRun(); }}
      />
      {childRunId && (
        <AgentRunDetailDrawer
          key={childRunId}
          initialRunId={childRunId}
          depth={(props.depth ?? 0) + 1}
          onClose={() => setChildRunId(null)}
          onRefresh={props.onRefresh}
        />
      )}
    </>
  );
}

type ExecutionSelection = { kind: "agent" | "preflight"; id: string };

function RunsView(props: {
  runs: RunSummary[];
  preflightRuns: PreflightRunSummary[];
  repositories: Repository[];
  filter: ExecutionFilter;
  requestedRunId?: string | null;
  onRequestedRunOpened: () => void;
  onFilterChange: (filter: ExecutionFilter) => void;
  onRefresh: () => void;
}) {
  const [selected, setSelected] = useState<ExecutionSelection | null>(null);
  const predefinedLimits = [10, 20, 50];
  const limitMode = props.filter.limit === null
    ? "all"
    : predefinedLimits.includes(props.filter.limit)
      ? String(props.filter.limit)
      : "custom";
  const [customLimit, setCustomLimit] = useState(
    props.filter.limit !== null && !predefinedLimits.includes(props.filter.limit)
      ? String(props.filter.limit)
      : "100",
  );

  useEffect(() => {
    if (props.filter.limit !== null && !predefinedLimits.includes(props.filter.limit)) {
      setCustomLimit(String(props.filter.limit));
    }
  }, [props.filter.limit]);

  function applyCustomLimit() {
    const parsed = Number(customLimit);
    if (Number.isInteger(parsed) && parsed > 0) {
      props.onFilterChange({ ...props.filter, limit: parsed });
      return;
    }
    const fallback = props.filter.limit !== null && props.filter.limit > 0
      ? props.filter.limit
      : 100;
    setCustomLimit(String(fallback));
  }

  const records = useMemo<Array<{
    kind: "agent" | "preflight";
    id: string;
    startedAt: number;
    agent?: RunSummary;
    preflight?: PreflightRunSummary;
  }>>(() => {
    const agentRecords = props.runs.map((agent) => ({
      kind: "agent" as const,
      id: agent.run_id,
      startedAt: agent.started_at,
      agent,
    }));
    const preflightRecords = props.preflightRuns.map((preflight) => ({
      kind: "preflight" as const,
      id: preflight.run_id,
      startedAt: preflight.started_at,
      preflight,
    }));
    return [...agentRecords, ...preflightRecords]
      .filter((record) => props.filter.type === "all" || record.kind === props.filter.type)
      .filter((record) => {
        const repositoryId = record.kind === "agent"
          ? record.agent.repository_id
          : record.preflight.repository_id;
        return !props.filter.repositoryId || repositoryId === props.filter.repositoryId;
      })
      .filter((record) => {
        if (!/^\d+$/.test(props.filter.number) || Number(props.filter.number) <= 0) {
          return true;
        }
        const number = record.kind === "agent"
          ? record.agent.change_request_number
          : record.preflight.number;
        return number === Number(props.filter.number);
      })
      .filter((record) => executionStatusMatches(
        record.kind,
        record.kind === "agent" ? record.agent.status : record.preflight.status,
        props.filter.statuses,
      ))
      .sort((left, right) => right.startedAt - left.startedAt)
      .slice(0, props.filter.limit ?? Number.MAX_SAFE_INTEGER);
  }, [props.filter, props.preflightRuns, props.runs]);

  useEffect(() => {
    if (!props.requestedRunId) return;
    setSelected({ kind: "agent", id: props.requestedRunId });
    props.onRequestedRunOpened();
  }, [props.requestedRunId, props.onRequestedRunOpened]);

  return (
    <>
      <section className="section-card runs-list">
        <div className="section-title-row">
          <div><h2>执行记录</h2><p>按开始时间统一查看 Agent 与本地 Preflight / CI；点击一行打开详情。</p></div>
          <div className="runs-list-actions">
            <SelectField
              className="runs-repository-filter"
              label="仓库"
              value={props.filter.repositoryId}
              onChange={(repositoryId) => props.onFilterChange({
                ...props.filter,
                repositoryId,
              })}
              options={[
                { value: "", label: "全部仓库" },
                ...props.repositories.map((repository) => ({
                  value: repository.id,
                  label: `${repository.id} · ${repository.project}`,
                })),
              ]}
            />
            <label className="runs-number-filter">
              <span>编号</span>
              <input
                type="number"
                min="1"
                step="1"
                inputMode="numeric"
                placeholder="全部"
                value={props.filter.number}
                onChange={(event) => props.onFilterChange({
                  ...props.filter,
                  number: event.target.value,
                })}
              />
            </label>
            <SelectField
              label="类型"
              value={props.filter.type}
              onChange={(value) => props.onFilterChange({
                ...props.filter,
                type: value as ExecutionTypeFilter,
              })}
              options={[
                { value: "all", label: "全部" },
                { value: "agent", label: "Agent" },
                { value: "preflight", label: "本地 CI" },
              ]}
            />
            <MultiSelectField
              label="状态"
              values={props.filter.statuses}
              onChange={(statuses) => props.onFilterChange({
                ...props.filter,
                statuses: statuses as ExecutionStatusFilter[],
              })}
              options={EXECUTION_STATUS_OPTIONS}
              allLabel="全部状态"
            />
            <SelectField
              label="展示"
              value={limitMode}
              onChange={(value) => {
                if (value === "all") {
                  props.onFilterChange({ ...props.filter, limit: null });
                } else if (value === "custom") {
                  const parsed = Number(customLimit);
                  props.onFilterChange({
                    ...props.filter,
                    limit: Number.isInteger(parsed) && parsed > 0 ? parsed : 100,
                  });
                } else {
                  props.onFilterChange({
                    ...props.filter,
                    limit: Number(value),
                  });
                }
              }}
              options={[
                { value: "10", label: "10 条" },
                { value: "20", label: "20 条" },
                { value: "50", label: "50 条" },
                { value: "all", label: "全部" },
                { value: "custom", label: "自定义" },
              ]}
            />
            {limitMode === "custom" && (
              <label className="runs-custom-limit">
                <span>自定义条数</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  inputMode="numeric"
                  value={customLimit}
                  onChange={(event) => setCustomLimit(event.target.value)}
                  onBlur={applyCustomLimit}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      applyCustomLimit();
                      event.currentTarget.blur();
                    }
                  }}
                />
              </label>
            )}
            <button className="button secondary" onClick={props.onRefresh}>刷新</button>
          </div>
        </div>
        <div className="run-table">
          <div className="run-table-head" aria-hidden="true">
            <span>类型 / 名称</span><span>仓库 / MR / PR</span><span>触发来源</span><span>状态</span><span>开始时间</span><span>耗时</span><span />
          </div>
          <div className="run-items">
            {records.map((record) => {
              if (record.agent) {
                const run = record.agent;
                return (
                  <button key={`agent:${run.run_id}`} className={`run-row ${selected?.kind === "agent" && selected.id === run.run_id ? "selected" : ""}`} onClick={() => setSelected({ kind: "agent", id: run.run_id })}>
                    <span className="run-agent-cell"><span className="run-status-dot" data-status={run.status} /><span><strong>{run.agent_name}</strong><small>Agent · {run.run_id.slice(0, 8)}</small></span></span>
                    <span className="run-target-cell"><strong>{runTargetText(run)}</strong><small>{run.change_request_title ?? run.resource_key}</small></span>
                    <span className="run-source-cell"><strong>{run.rule_name ?? "Sub-agent 调用"}</strong><small>{run.parent_run_id ? "Sub-agent" : "根 Agent"}</small></span>
                    <span className="run-status-cell"><StatusPill value={run.status} />{run.status === "queued" && queueReasonLabel(run.queue_reason) && <small>{queueReasonLabel(run.queue_reason)}</small>}{run.workspace_status === "retained" && <em className="workspace-retained">工作区待清理</em>}</span>
                    <span className="run-time-cell"><strong>{timeText(run.started_at)}</strong></span>
                    <span className="run-duration-cell"><strong>{durationText(run.started_at, run.finished_at)}</strong></span>
                    <span className="run-row-arrow" aria-hidden="true">›</span>
                  </button>
                );
              }
              const preflight = record.preflight!;
              return (
                <button key={`preflight:${preflight.run_id}`} className={`run-row ${selected?.kind === "preflight" && selected.id === preflight.run_id ? "selected" : ""}`} onClick={() => setSelected({ kind: "preflight", id: preflight.run_id })}>
                  <span className="run-agent-cell"><span className="run-status-dot" data-status={preflight.status} /><span><strong>本地 Preflight / CI</strong><small>CI · {preflight.run_id.slice(0, 8)}</small></span></span>
                  <span className="run-target-cell"><strong>{preflight.repository_id} · {preflight.number ? `#${preflight.number}` : preflight.branch ?? "默认分支"}</strong><small>{preflight.change_request_title ?? preflight.head_sha}</small></span>
                  <span className="run-source-cell"><strong>{preflight.trigger_source === "manual" ? "仓库手动执行" : preflight.event_type ?? "事件检查"}</strong><small>{preflight.trigger_source === "manual" ? "不触发 Agent、不回写 PR 状态" : preflight.reused_event_count > 0 ? `被 ${preflight.reused_event_count} 个事件复用` : "本批次新执行"}</small></span>
                  <span className="run-status-cell"><span className={`status-pill status-${preflightStatusClass(preflight.status)}`}>{preflightStatusLabel(preflight.status)}</span>{preflight.failed_step && <small>{preflight.failed_step}</small>}</span>
                  <span className="run-time-cell"><strong>{timeText(preflight.started_at)}</strong></span>
                  <span className="run-duration-cell"><strong>{durationText(preflight.started_at, preflight.finished_at)}</strong></span>
                  <span className="run-row-arrow" aria-hidden="true">›</span>
                </button>
              );
            })}
            {records.length === 0 && <div className="empty">当前筛选下尚无执行记录</div>}
          </div>
        </div>
      </section>
      {selected?.kind === "agent" && <AgentRunDetailDrawer initialRunId={selected.id} onClose={() => setSelected(null)} onRefresh={props.onRefresh} />}
      {selected?.kind === "preflight" && <PreflightRunDetailDrawer runId={selected.id} depth={0} onClose={() => setSelected(null)} />}
    </>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("overview");
  const [document, setDocument] = useState<ConfigDocument | null>(null);
  const [savedDocument, setSavedDocument] = useState<ConfigDocument | null>(null);
  const [status, setStatus] = useState<RuntimeStatus>(EMPTY_STATUS);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [preflightRuns, setPreflightRuns] = useState<PreflightRunSummary[]>([]);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [changeRequests, setChangeRequests] = useState<ChangeRequestRecord[]>([]);
  const [changeRequestPage, setChangeRequestPage] = useState<OverviewPage>(
    EMPTY_OVERVIEW_PAGE,
  );
  const [eventPage, setEventPage] = useState<OverviewPage>(EMPTY_OVERVIEW_PAGE);
  const [changeRequestFilter, setChangeRequestFilter] = useState<OverviewFilter>(
    DEFAULT_OVERVIEW_FILTER,
  );
  const [eventFilter, setEventFilter] = useState<OverviewFilter>(
    DEFAULT_OVERVIEW_FILTER,
  );
  const [executionFilter, setExecutionFilter] = useState<ExecutionFilter>(
    DEFAULT_EXECUTION_FILTER,
  );
  const executionFilterRef = useRef<ExecutionFilter>(DEFAULT_EXECUTION_FILTER);
  const operationalRequestSequence = useRef(0);
  const overviewRequestSequence = useRef(0);
  const mainRef = useRef<HTMLElement>(null);
  const [emittingKey, setEmittingKey] = useState("");
  const [triggeringKeys, setTriggeringKeys] = useState<string[]>([]);
  const [changeRequestSelectionMode, setChangeRequestSelectionMode] = useState(false);
  const [selectedSnapshotKeys, setSelectedSnapshotKeys] = useState<string[]>([]);
  const [overviewConfirmation, setOverviewConfirmation] = useState<OverviewConfirmation | null>(null);
  const [confirmingOverviewAction, setConfirmingOverviewAction] = useState(false);
  const [eventOptions, setEventOptions] = useState<string[]>([]);
  const [codexOptions, setCodexOptions] = useState<CodexRuntimeOptions>(EMPTY_CODEX_OPTIONS);
  const [revision, setRevision] = useState("");
  const [editing, setEditing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [agentDetailDirty, setAgentDetailDirty] = useState(false);
  const [pendingAgentExitTab, setPendingAgentExitTab] = useState<Tab | null>(null);
  const [ruleDetailDirty, setRuleDetailDirty] = useState(false);
  const [pendingRuleExitTab, setPendingRuleExitTab] = useState<Tab | null>(null);
  const [repositoryDetailDirty, setRepositoryDetailDirty] = useState(false);
  const [repositoryDetailOpen, setRepositoryDetailOpen] = useState(false);
  const [pendingRepositoryExitTab, setPendingRepositoryExitTab] = useState<Tab | null>(null);
  const [requestedRunId, setRequestedRunId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [token, setToken] = useState(getToken());

  const refreshOperationalData = useCallback(async () => {
    const requestSequence = operationalRequestSequence.current + 1;
    operationalRequestSequence.current = requestSequence;
    const filter = { ...executionFilterRef.current };
    const query = executionQuery(filter);
    const runsRequest = filter.type === "preflight"
      ? Promise.resolve<RunSummary[]>([])
      : api<RunSummary[]>(`/api/runs?${query}`);
    const preflightRunsRequest = filter.type === "agent"
      ? Promise.resolve<PreflightRunSummary[]>([])
      : api<PreflightRunSummary[]>(`/api/preflight-runs?${query}`);
    const [nextStatus, nextRuns, nextPreflightRuns] = await Promise.all([
      api<RuntimeStatus>("/api/status"),
      runsRequest,
      preflightRunsRequest,
    ]);
    if (requestSequence !== operationalRequestSequence.current) return;
    setStatus(nextStatus);
    setRuns(nextRuns);
    setPreflightRuns(nextPreflightRuns);
  }, []);

  const refreshOverviewData = useCallback(async () => {
    const requestSequence = overviewRequestSequence.current + 1;
    overviewRequestSequence.current = requestSequence;
    const [nextEvents, nextChangeRequests] = await Promise.all([
      api<PaginatedOverviewResponse<EventRecord>>(`/api/events?${overviewQuery(eventFilter, true)}`),
      api<PaginatedOverviewResponse<ChangeRequestRecord>>(`/api/change-requests?${overviewQuery(changeRequestFilter)}`),
    ]);
    if (requestSequence !== overviewRequestSequence.current) return;
    setEvents(nextEvents.items);
    setChangeRequests(nextChangeRequests.items);
    setEventPage(nextEvents);
    setChangeRequestPage(nextChangeRequests);
    if (nextEvents.page !== eventFilter.page) {
      setEventFilter((current) => ({ ...current, page: nextEvents.page }));
    }
    if (nextChangeRequests.page !== changeRequestFilter.page) {
      setChangeRequestFilter((current) => ({ ...current, page: nextChangeRequests.page }));
    }
  }, [changeRequestFilter, eventFilter]);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [config, options, codexResult] = await Promise.all([
        api<{ revision: string; document: ConfigDocument; error?: string }>("/api/config"),
        api<{ events: string[] }>("/api/options"),
        loadCodexRuntimeOptions(),
        refreshOperationalData(),
      ]);
      const normalized = normalizeDocument(config.document);
      setDocument(normalized);
      setSavedDocument(structuredClone(normalized));
      setRevision(config.revision);
      setEventOptions(options.events);
      setCodexOptions(codexResult.options);
      setDirty(false);
      setEditing(false);
      setAgentDetailDirty(false);
      setRuleDetailDirty(false);
      setRepositoryDetailDirty(false);
      setRepositoryDetailOpen(false);
      const diagnosticErrors = [config.error, codexResult.error].filter(Boolean);
      if (diagnosticErrors.length > 0) setError(diagnosticErrors.join("；"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [refreshOperationalData]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    executionFilterRef.current = executionFilter;
    void refreshOperationalData().catch(() => undefined);
  }, [executionFilter, refreshOperationalData]);
  useEffect(() => {
    const timer = window.setInterval(() => { void refreshOperationalData().catch(() => undefined); }, 3000);
    return () => window.clearInterval(timer);
  }, [refreshOperationalData]);
  useEffect(() => {
    void refreshOverviewData().catch((reason) => {
      setError(reason instanceof Error ? reason.message : "概览列表加载失败");
    });
    const timer = window.setInterval(() => { void refreshOverviewData().catch(() => undefined); }, 3000);
    return () => window.clearInterval(timer);
  }, [refreshOverviewData]);
  useEffect(() => {
    const availableKeys = new Set(
      changeRequests
        .filter((item) => item.latest_event)
        .map((item) => item.snapshot_key),
    );
    setSelectedSnapshotKeys((current) => {
      const next = current.filter((key) => availableKeys.has(key));
      return next.length === current.length ? current : next;
    });
  }, [changeRequests]);

  function changeDocument(next: ConfigDocument) {
    if (!editing) return;
    setDocument(next);
    setDirty(true);
    setNotice("");
  }

  function selectTab(nextTab: Tab) {
    if (nextTab === tab) return;
    if (tab === "agents" && agentDetailDirty) {
      setPendingAgentExitTab(nextTab);
      return;
    }
    if (tab === "rules" && ruleDetailDirty) {
      setPendingRuleExitTab(nextTab);
      return;
    }
    if (tab === "repositories" && repositoryDetailDirty) {
      setPendingRepositoryExitTab(nextTab);
      return;
    }
    if (tab === "overview") cancelChangeRequestSelection();
    setTab(nextTab);
  }

  function acceptItemConfig(nextDocument: ConfigDocument, nextRevision: string) {
    setDocument(nextDocument);
    setSavedDocument(structuredClone(nextDocument));
    setRevision(nextRevision);
    setAgentDetailDirty(false);
    setRuleDetailDirty(false);
    setRepositoryDetailDirty(false);
    setError("");
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
      const codexResult = await loadCodexRuntimeOptions();
      setCodexOptions(codexResult.options);
      setDirty(false);
      setEditing(false);
      if (codexResult.error) setError(codexResult.error);
      setNotice(
        codexResult.error
          ? "配置已校验、保存并通知后台热加载；Codex 运行时诊断刷新失败"
          : "配置已校验、保存并通知后台热加载",
      );
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

  function cancelChangeRequestSelection() {
    setChangeRequestSelectionMode(false);
    setSelectedSnapshotKeys([]);
  }

  function changeOverviewChangeRequestFilter(filter: OverviewFilter) {
    cancelChangeRequestSelection();
    setChangeRequestFilter({ ...filter, page: 1 });
  }

  function changeOverviewEventFilter(filter: OverviewFilter) {
    setEventFilter({ ...filter, page: 1 });
  }

  function changeOverviewChangeRequestPage(page: number) {
    cancelChangeRequestSelection();
    setChangeRequestFilter((current) => ({ ...current, page }));
  }

  function changeOverviewEventPage(page: number) {
    setEventFilter((current) => ({ ...current, page }));
  }

  function toggleChangeRequestSelection(snapshotKey: string) {
    setSelectedSnapshotKeys((current) => (
      current.includes(snapshotKey)
        ? current.filter((key) => key !== snapshotKey)
        : [...current, snapshotKey]
    ));
  }

  function requestEmitDiscovered(item: ChangeRequestRecord) {
    const hasMatchingRule = Boolean(document?.rules.some((rule) => (
      rule.enabled !== false
      && rule.events.includes("change_request.discovered")
      && (!rule.repositories || rule.repositories.includes(item.repository_id))
    )));
    setOverviewConfirmation({
      kind: "discovered",
      items: [item],
      eyebrow: "首次事件",
      title: "确认补发首次事件",
      description: "请核对目标与规则影响。确认后会创建一条首次发现事件。",
      details: [
        { label: "目标", value: `${item.repository_id} · #${item.number} ${item.title}` },
        { label: "事件", value: "change_request.discovered", mono: true },
        { label: "当前状态", value: item.state },
      ],
      impactTitle: hasMatchingRule ? "可能触发 Agent" : "当前没有候选规则",
      impact: hasMatchingRule
        ? "存在事件和仓库范围可能匹配的启用规则；最终仍需满足规则条件，Agent 可能执行其获准操作。"
        : "本次事件预计会被记录为未触发，不会启动 Agent。",
      impactTone: hasMatchingRule ? "attention" : "quiet",
      safetyNote: "确认动作本身不会直接修改远端 PR；后续 Agent 行为受规则权限控制。",
      confirmLabel: "确认补发",
    });
  }

  function requestTriggerLatestEvent(item: ChangeRequestRecord) {
    const latestEvent = item.latest_event;
    if (!latestEvent) return;
    const hasCandidateRule = Boolean(document?.rules.some((rule) => (
      rule.enabled !== false
      && rule.events.includes(latestEvent.event_type)
      && (!rule.repositories || rule.repositories.includes(item.repository_id))
    )));
    setOverviewConfirmation({
      kind: "latest",
      items: [item],
      eyebrow: "手动事件",
      title: "确认手动触发",
      description: "将缓存的最新平台事件重新发送到当前规则引擎。",
      details: [
        { label: "目标", value: `${item.repository_id} · #${item.number} ${item.title}` },
        { label: "事件", value: latestEvent.event_type, mono: true },
        { label: "平台原事件时间", value: dateTimeText(latestEvent.occurred_at) },
      ],
      impactTitle: hasCandidateRule ? "可能触发 Agent" : "当前没有候选规则",
      impact: hasCandidateRule
        ? "存在事件和仓库范围可能匹配的启用规则；最终仍需满足规则条件，Agent 可能执行其获准操作。"
        : "本次事件预计会被记录为未触发，不会启动 Agent。",
      impactTone: hasCandidateRule ? "attention" : "quiet",
      safetyNote: "确认动作本身不会直接修改远端 PR；后续 Agent 行为受规则权限控制。",
      confirmLabel: "确认触发",
    });
  }

  function requestTriggerLatestEvents(items: ChangeRequestRecord[]) {
    const targets = items.filter((item) => item.latest_event);
    if (targets.length === 0) return;

    const repositoryCounts = new Map<string, number>();
    const eventCounts = new Map<string, number>();
    let candidateTargetCount = 0;
    for (const item of targets) {
      const eventType = item.latest_event?.event_type;
      if (!eventType) continue;
      repositoryCounts.set(item.repository_id, (repositoryCounts.get(item.repository_id) ?? 0) + 1);
      eventCounts.set(eventType, (eventCounts.get(eventType) ?? 0) + 1);
      if (document?.rules.some((rule) => (
        rule.enabled !== false
        && rule.events.includes(eventType)
        && (!rule.repositories || rule.repositories.includes(item.repository_id))
      ))) {
        candidateTargetCount += 1;
      }
    }
    const summarize = (counts: Map<string, number>) => (
      [...counts.entries()].map(([name, count]) => `${name} × ${count}`).join("；")
    );
    setOverviewConfirmation({
      kind: "latest-batch",
      items: targets,
      eyebrow: "批量手动事件",
      title: `确认触发 ${targets.length} 个 MR / PR`,
      description: "每个目标将使用自己的缓存最新事件，分别发送到当前规则引擎。",
      details: [
        { label: "目标数量", value: `${targets.length} 个` },
        { label: "仓库分布", value: summarize(repositoryCounts) },
        { label: "事件分布", value: summarize(eventCounts), mono: true },
      ],
      impactTitle: candidateTargetCount > 0
        ? `${candidateTargetCount} 个目标可能触发 Agent`
        : "当前没有候选规则",
      impact: candidateTargetCount > 0
        ? "存在事件和仓库范围可能匹配的启用规则；每个目标仍需分别满足规则条件，Agent 可能执行其获准操作。"
        : "这些事件预计会被记录为未触发，不会启动 Agent。",
      impactTone: candidateTargetCount > 0 ? "attention" : "quiet",
      safetyNote: "确认动作本身不会直接修改远端 PR；后续 Agent 行为受规则权限控制。",
      confirmLabel: `触发 ${targets.length} 项`,
    });
  }

  function closeOverviewConfirmation() {
    if (!confirmingOverviewAction) setOverviewConfirmation(null);
  }

  async function confirmOverviewAction() {
    const action = overviewConfirmation;
    if (!action || confirmingOverviewAction) return;

    setConfirmingOverviewAction(true);
    setError("");
    try {
      const item = action.items[0];
      if (!item) return;
      if (action.kind === "discovered") {
        setEmittingKey(item.snapshot_key);
      } else {
        setTriggeringKeys(action.items.map((target) => target.snapshot_key));
      }
      let result: { created: boolean; reason: string } | ManualLatestEventBatchResponse;
      if (action.kind === "latest-batch") {
        result = await api<ManualLatestEventBatchResponse>(
          "/api/change-requests/trigger-latest-events",
          {
            method: "POST",
            body: JSON.stringify({
              targets: action.items.map((target) => ({
                repository_id: target.repository_id,
                number: target.number,
              })),
            }),
          },
        );
      } else {
        const endpoint = action.kind === "discovered"
          ? `/api/change-requests/${encodeURIComponent(item.repository_id)}/${item.number}/emit-discovered`
          : `/api/change-requests/${encodeURIComponent(item.repository_id)}/${item.number}/trigger-latest-event`;
        result = await api<{ created: boolean; reason: string }>(endpoint, { method: "POST" });
      }
      await Promise.all([refreshOperationalData(), refreshOverviewData()]);
      setNotice(result.reason);
      if (action.kind === "latest-batch" && "results" in result) {
        const failed = result.results.filter((entry) => !entry.created);
        if (failed.length === 0) {
          cancelChangeRequestSelection();
        } else {
          const failedKeys = new Set(
            failed.map((entry) => `${entry.repository_id}:${entry.number}`),
          );
          setSelectedSnapshotKeys(
            action.items
              .filter((target) => failedKeys.has(`${target.repository_id}:${target.number}`))
              .map((target) => target.snapshot_key),
          );
          setError(
            `有 ${failed.length} 项触发失败：${failed
              .map((entry) => `${entry.repository_id} #${entry.number}（${entry.reason}）`)
              .join("；")}`,
          );
        }
      }
      window.setTimeout(() => setNotice(""), 3500);
    } catch (reason) {
      const fallback = action.kind === "discovered"
        ? "补发首次发现事件失败"
        : "手动触发最新事件失败";
      setError(reason instanceof Error ? reason.message : fallback);
    } finally {
      setOverviewConfirmation(null);
      setConfirmingOverviewAction(false);
      setEmittingKey("");
      setTriggeringKeys([]);
    }
  }

  const enabledRepositories = useMemo(
    () => (savedDocument?.repositories ?? document?.repositories ?? [])
      .filter((repository) => repository.enabled !== false),
    [document?.repositories, savedDocument?.repositories],
  );

  useEffect(() => {
    const enabledIds = new Set(enabledRepositories.map((repository) => repository.id));
    setChangeRequestFilter((current) => (
      current.repositoryId && !enabledIds.has(current.repositoryId)
        ? { ...current, repositoryId: "", page: 1 }
        : current
    ));
    setEventFilter((current) => (
      current.repositoryId && !enabledIds.has(current.repositoryId)
        ? { ...current, repositoryId: "", page: 1 }
        : current
    ));
    setExecutionFilter((current) => (
      current.repositoryId && !enabledIds.has(current.repositoryId)
        ? { ...current, repositoryId: "" }
        : current
    ));
  }, [enabledRepositories]);

  const tabs = useMemo<Array<{ id: Tab; label: string; mark: string }>>(() => [
    { id: "overview", label: "运行概览", mark: "01" },
    { id: "repositories", label: "仓库", mark: "02" },
    { id: "environment", label: "全局配置与环境", mark: "03" },
    { id: "model-providers", label: "Provider", mark: "04" },
    { id: "skills", label: "SKILL", mark: "05" },
    { id: "agents", label: "Agent", mark: "06" },
    { id: "rules", label: "触发规则", mark: "07" },
    { id: "runs", label: "运行与日志", mark: "08" },
  ], []);
  const configurableTab = (
    tab === "environment"
    || tab === "skills"
  );

  useLayoutEffect(() => {
    globalThis.document.scrollingElement?.scrollTo({
      top: 0,
      left: 0,
      behavior: "auto",
    });
    mainRef.current?.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [tab]);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><div className="brand-mark">TR</div><div><strong>Teamwork</strong><span>Review Agents</span></div></div>
        <nav>{tabs.map((item) => <button key={item.id} className={tab === item.id ? "active" : ""} onClick={() => selectTab(item.id)}><span>{item.mark}</span>{item.label}</button>)}</nav>
        <div className="sidebar-footer"><span className={`service-dot ${status.paused ? "paused" : status.running_cycle || status.dispatching_events ? "busy" : ""}`} /><div><strong>{status.paused ? "已暂停" : "后台在线"}</strong><small>rev {shortRevision(revision || status.config_revision)}</small></div></div>
      </aside>
      <main className="main" ref={mainRef}>
        <header className="topbar">
          <div><span className="eyebrow">MR / PR AUTOMATION</span><h1>{tabs.find((item) => item.id === tab)?.label}</h1></div>
          <div className="top-actions">
            <label className="token-field"><span>管理 Token</span><input type="password" value={token} placeholder="本机模式可留空" onChange={(event) => setToken(event.target.value)} onBlur={() => { persistToken(token); if (!editing) void load(); }} /></label>
            {tab !== "model-providers" && (configurableTab || editing) && (
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
                  repositories={enabledRepositories}
                  changeRequestFilter={changeRequestFilter}
                  eventFilter={eventFilter}
                  changeRequestPage={changeRequestPage}
                  eventPage={eventPage}
                  emittingKey={emittingKey}
                  triggeringKeys={triggeringKeys}
                  selectionMode={changeRequestSelectionMode}
                  selectedSnapshotKeys={selectedSnapshotKeys}
                  onAction={control}
                  onEmitDiscovered={requestEmitDiscovered}
                  onTriggerLatestEvent={requestTriggerLatestEvent}
                  onBeginSelection={() => setChangeRequestSelectionMode(true)}
                  onCancelSelection={cancelChangeRequestSelection}
                  onToggleSelection={toggleChangeRequestSelection}
                  onTriggerSelected={requestTriggerLatestEvents}
                  onChangeRequestFilterChange={changeOverviewChangeRequestFilter}
                  onEventFilterChange={changeOverviewEventFilter}
                  onChangeRequestPageChange={changeOverviewChangeRequestPage}
                  onEventPageChange={changeOverviewEventPage}
                />
              )}
              {configurableTab && (
                <>
                  <div className={`edit-mode-banner ${editing ? "editing" : ""}`}>
                    <span>{editing ? "编辑模式" : "只读模式"}</span>
                    <small>{editing ? "修改会暂存在页面中，请使用右上角保存或取消。" : "点击右上角“编辑配置”后才能修改。"}</small>
                  </div>
                  <fieldset className="config-editor-surface" disabled={!editing}>
                    {tab === "environment" && <GlobalEnvironment document={document} codexOptions={codexOptions} onChange={changeDocument} />}
                    {tab === "skills" && <SkillsEditor document={document} onChange={changeDocument} />}
                  </fieldset>
                  {tab === "environment" && <ConfigHistory />}
                </>
              )}
              {tab === "model-providers" && (
                <ModelProvidersEditor
                  document={document}
                  revision={revision}
                  codexOptions={codexOptions}
                  onSaved={acceptItemConfig}
                  onError={setError}
                  onNotice={(message) => {
                    setNotice(message);
                    window.setTimeout(() => setNotice(""), 3500);
                  }}
                />
              )}
              <div hidden={tab !== "repositories" || repositoryDetailOpen}>
                <RepositoryConnectionsEditor
                  document={document}
                  revision={revision}
                  onSaved={acceptItemConfig}
                  onError={setError}
                  onNotice={(message) => {
                    setNotice(message);
                    window.setTimeout(() => setNotice(""), 3500);
                  }}
                />
              </div>
              {tab === "agents" && (
                <AgentsView
                  document={document}
                  revision={revision}
                  codexOptions={codexOptions}
                  onSaved={acceptItemConfig}
                  onDirtyChange={setAgentDetailDirty}
                  onError={setError}
                  onNotice={(message) => {
                    setNotice(message);
                    window.setTimeout(() => setNotice(""), 3500);
                  }}
                />
              )}
              {tab === "repositories" && (
                <RepositoriesView
                  document={document}
                  revision={revision}
                  onSaved={acceptItemConfig}
                  onDirtyChange={setRepositoryDetailDirty}
                  onDetailOpenChange={setRepositoryDetailOpen}
                  onError={setError}
                  onNotice={(message) => {
                    setNotice(message);
                    window.setTimeout(() => setNotice(""), 3500);
                  }}
                  onOpenRun={(runId) => {
                    setRequestedRunId(runId);
                    setTab("runs");
                  }}
                />
              )}
              {tab === "rules" && (
                <RulesView
                  document={document}
                  revision={revision}
                  events={eventOptions}
                  onSaved={acceptItemConfig}
                  onDirtyChange={setRuleDetailDirty}
                  onError={setError}
                  onNotice={(message) => {
                    setNotice(message);
                    window.setTimeout(() => setNotice(""), 3500);
                  }}
                />
              )}
              {tab === "runs" && (
                <RunsView
                  runs={runs}
                  preflightRuns={preflightRuns}
                  repositories={enabledRepositories}
                  filter={executionFilter}
                  requestedRunId={requestedRunId}
                  onRequestedRunOpened={() => setRequestedRunId(null)}
                  onFilterChange={setExecutionFilter}
                  onRefresh={() => { void refreshOperationalData(); }}
                />
              )}
            </>
          )}
        </div>
      </main>
      <OverviewConfirmationDialog
        model={overviewConfirmation}
        busy={confirmingOverviewAction}
        onCancel={closeOverviewConfirmation}
        onConfirm={() => { void confirmOverviewAction(); }}
      />
      <AgentActionConfirmationDialog
        model={pendingAgentExitTab ? {
          eyebrow: "AGENT CONFIGURATION",
          title: "离开 Agent 编辑页？",
          description: "当前 Agent 仍有未保存修改，离开后页面草稿将被放弃。",
          details: [
            { label: "即将前往", value: tabs.find((item) => item.id === pendingAgentExitTab)?.label ?? pendingAgentExitTab },
            { label: "配置版本", value: shortRevision(revision), mono: true },
          ],
          impactTitle: "已保存配置不会变化",
          impact: "只会放弃当前 Agent 的页面草稿，后台运行状态不会受到影响。",
          confirmLabel: "放弃并离开",
          dangerous: true,
        } : null}
        onCancel={() => setPendingAgentExitTab(null)}
        onConfirm={() => {
          if (!pendingAgentExitTab) return;
          setAgentDetailDirty(false);
          setTab(pendingAgentExitTab);
          setPendingAgentExitTab(null);
        }}
      />
      <AgentActionConfirmationDialog
        model={pendingRuleExitTab ? {
          eyebrow: "TRIGGER RULE",
          title: "离开规则编辑页？",
          description: "当前规则仍有未保存修改，离开后页面草稿将被放弃。",
          details: [
            { label: "即将前往", value: tabs.find((item) => item.id === pendingRuleExitTab)?.label ?? pendingRuleExitTab },
            { label: "配置版本", value: shortRevision(revision), mono: true },
          ],
          impactTitle: "已保存配置不会变化",
          impact: "只会放弃当前规则的页面草稿，历史事件和后台运行状态不会受到影响。",
          confirmLabel: "放弃并离开",
          dangerous: true,
        } : null}
        onCancel={() => setPendingRuleExitTab(null)}
        onConfirm={() => {
          if (!pendingRuleExitTab) return;
          setRuleDetailDirty(false);
          setTab(pendingRuleExitTab);
          setPendingRuleExitTab(null);
        }}
      />
      <AgentActionConfirmationDialog
        model={pendingRepositoryExitTab ? {
          eyebrow: "REPOSITORY CONFIGURATION",
          title: "离开仓库编辑页？",
          description: "当前仓库仍有未保存修改，离开后页面草稿将被放弃。",
          details: [
            { label: "即将前往", value: tabs.find((item) => item.id === pendingRepositoryExitTab)?.label ?? pendingRepositoryExitTab },
            { label: "配置版本", value: shortRevision(revision), mono: true },
          ],
          impactTitle: "已保存配置和本地目录不会变化",
          impact: "只会放弃当前仓库的页面草稿，基础仓库、历史事件和后台运行状态不会受到影响。",
          confirmLabel: "放弃并离开",
          dangerous: true,
        } : null}
        onCancel={() => setPendingRepositoryExitTab(null)}
        onConfirm={() => {
          if (!pendingRepositoryExitTab) return;
          setRepositoryDetailDirty(false);
          setRepositoryDetailOpen(false);
          setTab(pendingRepositoryExitTab);
          setPendingRepositoryExitTab(null);
        }}
      />
    </div>
  );
}
