export type EnvironmentVariable = {
  value?: string;
  from_system?: string;
  secret?: boolean;
  expose_to_prompt?: boolean;
  expose_to_process?: boolean;
};

export type EnvironmentMap = Record<string, EnvironmentVariable | string | number | boolean | null>;

export type RepositoryPreflightStep = {
  name: string;
  command: string[];
  timeout_seconds?: number;
};

export type RepositoryPreflight = {
  enabled?: boolean;
  status_context?: string;
  timeout_seconds?: number;
  max_output_bytes?: number;
  steps?: RepositoryPreflightStep[];
};

export type Repository = {
  id: string;
  provider: string;
  project: string;
  workspace: string;
  clone_url?: string;
  enabled?: boolean;
  environment?: EnvironmentMap;
  preflight?: RepositoryPreflight;
};

export type RepositoryWorkspaceStatus = {
  repository_id: string;
  workspace: string;
  enabled: boolean;
  ready: boolean;
  status: "uninitialized" | "invalid" | "ready" | "waiting" | "initializing" | "updating" | "failed" | "cancelled";
  operation?: "initialize" | "update" | null;
  phase: string;
  elapsed_seconds: number;
  started_at?: number | null;
  finished_at?: number | null;
  size_bytes?: number | null;
  error?: string | null;
  cancel_requested: boolean;
  detail_available: boolean;
  detail_source?: "manual" | "agent" | null;
  detail_run_id?: string | null;
};

export type GitCommandDetail = {
  command_id: string;
  operation: string;
  command: string;
  state: "waiting" | "started" | "progress" | "completed" | "failed" | "timed_out" | "cancelled";
  elapsed_seconds: number;
  timeout_seconds: number;
  started_at: number;
  finished_at?: number | null;
  exit_code?: number | null;
  error?: string | null;
};

export type RepositoryGitDetail = {
  repository_id: string;
  source?: "manual" | "agent" | null;
  run_id?: string | null;
  status: string;
  phase: string;
  started_at?: number | null;
  finished_at?: number | null;
  commands: GitCommandDetail[];
};

export type Agent = {
  prompt_file?: string;
  prompt?: string;
  model?: string;
  model_reasoning_effort?: string;
  fast_mode?: "inherit" | "standard" | "fast";
  model_verbosity?: "low" | "medium" | "high";
  personality?: "none" | "friendly" | "pragmatic";
  web_search?: "disabled" | "cached" | "live";
  sandbox?: "read-only" | "workspace-write" | "danger-full-access";
  home_mode?: "inherit" | "temporary";
  network_access?: boolean;
  network_domains?: string[];
  timeout_seconds?: number;
  idle_timeout_seconds?: number;
  write_scopes?: Array<"change_request" | "workspace">;
  allowed_sub_agents?: string[];
  skills?: string[];
  output_schema?: string;
  skip_git_repo_check?: boolean;
  extra_codex_args?: string[];
  environment?: EnvironmentMap;
};

export type CodexConfigValue = string | number | boolean | Array<string | number | boolean>;

export type CodexRuntimeConfig = {
  model?: string;
  model_reasoning_effort?: string;
  fast_mode?: "inherit" | "standard" | "fast";
  model_verbosity?: "low" | "medium" | "high";
  personality?: "none" | "friendly" | "pragmatic";
  web_search?: "disabled" | "cached" | "live";
  extra_config?: Record<string, CodexConfigValue>;
};

export type CodexInheritedSetting = {
  value?: string | null;
  source: "codex" | "user" | "model" | "builtin" | "runtime" | "unknown";
  known: boolean;
};

export type RuntimeConfig = Record<string, unknown> & {
  codex_binary?: string;
  codex_home?: string;
  expected_codex_version?: string;
  inherit_user_mcp_servers?: boolean;
  allowed_user_mcp_servers?: string[];
  repository_initialization_timeout_seconds?: number;
  git_timeout_seconds?: number;
  agent_idle_timeout_seconds?: number;
  codex?: CodexRuntimeConfig;
};

export type CodexRuntimeOptions = {
  models: Array<{
    slug: string;
    display_name: string;
    default_reasoning_level?: string | null;
    supported_reasoning_levels: string[];
    supports_fast_mode: boolean;
  }>;
  inherited_model: {
    value?: string | null;
    source: "runtime" | "codex" | "user" | "builtin";
    label: string;
  };
  codex_model?: string | null;
  codex_model_source: "codex" | "user" | "builtin";
  inherited_settings: {
    model_reasoning_effort: CodexInheritedSetting;
    fast_mode: CodexInheritedSetting;
    model_verbosity: CodexInheritedSetting;
    personality: CodexInheritedSetting;
    web_search: CodexInheritedSetting;
  };
  inherited_settings_error?: string | null;
  effective_config_error?: string | null;
  user_model?: string | null;
  user_config_path: string;
  catalog_error?: string | null;
  user_config_error?: string | null;
  codex_home: string;
  binary: {
    resolved_path?: string | null;
    version?: string | null;
    version_output?: string | null;
    error?: string | null;
  };
  model_cache: {
    path: string;
    client_version?: string | null;
    error?: string | null;
  };
  expected_version?: string | null;
  version_warning?: string | null;
  user_mcp_servers: string[];
  user_mcp_error?: string | null;
};

export type CodexRateLimitWindow = {
  usedPercent?: number;
  windowDurationMins?: number;
  resetsAt?: number;
};

export type CodexRateLimit = {
  limitId?: string;
  limitName?: string;
  planType?: string;
  rateLimitReachedType?: string;
  primary?: CodexRateLimitWindow;
  secondary?: CodexRateLimitWindow;
};

export type CodexAccountStatus = {
  managed: boolean;
  status: "inherited" | "signed_out" | "signed_in";
  codex_home?: string;
  requires_openai_auth?: boolean | null;
  account?: {
    type?: string;
    email?: string;
    planType?: string;
    credentialSource?: string;
  } | null;
  rate_limits?: {
    rateLimits?: CodexRateLimit;
    rateLimitsByLimitId?: CodexRateLimit[];
    rateLimitResetCredits?: { availableCount: number };
  } | null;
  usage?: {
    summary?: {
      lifetimeTokens?: number;
      peakDailyTokens?: number;
      longestRunningTurnSec?: number;
      currentStreakDays?: number;
      longestStreakDays?: number;
    };
  } | null;
  rate_limits_error?: string;
  usage_error?: string;
};

export type CodexLoginSession = {
  session_id: string;
  codex_home: string;
  auth_url: string;
  status: "pending" | "completed" | "failed" | "cancelled";
  error?: string | null;
  started_at: number;
  finished_at?: number | null;
};

export type Skill = {
  path: string;
};

export type Rule = {
  name: string;
  events: string[];
  agents: string[];
  repositories?: string[];
  conditions?: Record<string, unknown>;
  deduplicate_per_scan?: boolean;
  inherit_workspace?: boolean;
  run_preflight?: boolean;
  enabled?: boolean;
};

export type ConfigDocument = {
  database: { path: string };
  scanner: Record<string, unknown>;
  runtime: RuntimeConfig;
  web: Record<string, unknown>;
  environment: { global: EnvironmentMap };
  providers: Record<string, Record<string, unknown>>;
  repositories: Repository[];
  skills: Record<string, Skill>;
  agents: Record<string, Agent>;
  rules: Rule[];
};

export type RuntimeStatus = {
  paused: boolean;
  running_cycle: boolean;
  dispatching_events: boolean;
  config_revision: string;
  config_error?: string | null;
  last_started_at?: number | null;
  last_finished_at?: number | null;
  last_summary?: Record<string, unknown> | null;
  last_error?: string | null;
  last_dispatch_started_at?: number | null;
  last_dispatch_finished_at?: number | null;
  last_dispatch_summary?: Record<string, unknown> | null;
  last_dispatch_error?: string | null;
  stats: {
    runs: Record<string, number>;
    events: Record<string, number>;
    change_requests: Record<string, number>;
  };
};

export type ChangeRequestRecord = {
  snapshot_key: string;
  provider: string;
  repository_id: string;
  number: number;
  title: string;
  state: "opened" | "closed" | "merged";
  draft: boolean;
  source_branch: string;
  target_branch: string;
  head_sha: string;
  labels: string[];
  approvals: number;
  pipeline_status: string;
  merge_status: string;
  updated_at: string;
  web_url: string;
  scanned_at: number;
  discovered_event_emitted: boolean;
  latest_event_checked?: boolean;
  latest_event_supported?: boolean;
  latest_event?: {
    event_type: string;
    provider_event_type: string;
    provider_event_id: string;
    occurred_at?: string | null;
  } | null;
};

export type ManualLatestEventBatchResult = {
  repository_id: string;
  number: number;
  created: boolean;
  status_code: number;
  event_id?: string;
  event_type?: string;
  source_activity_id?: string;
  source_occurred_at?: string | null;
  reason: string;
};

export type ManualLatestEventBatchResponse = {
  requested: number;
  created: number;
  failed: number;
  results: ManualLatestEventBatchResult[];
  reason: string;
};

export type RunSummary = {
  run_id: string;
  root_run_id: string;
  parent_run_id?: string | null;
  event_id?: string | null;
  rule_name?: string | null;
  agent_name: string;
  resource_key: string;
  repository_id?: string | null;
  change_request_number?: number | null;
  change_request_title?: string | null;
  change_request_url?: string | null;
  status: string;
  attempts: number;
  error?: string | null;
  workspace_path?: string | null;
  workspace_status?: "active" | "removed" | "retained" | "inherited" | "not-created" | null;
  workspace_reason?: string | null;
  started_at: number;
  finished_at?: number | null;
};

export type RunDetail = RunSummary & {
  prompt: string;
  environment: Record<string, string>;
  config_revision?: string;
  final_message?: string | null;
  thread_id?: string | null;
  usage?: Record<string, unknown>;
  children: RunSummary[];
};

export type RunLog = {
  id: number;
  run_id: string;
  created_at: number;
  stream: string;
  event_type: string;
  payload: string;
};

export type EventRecord = {
  event_id: string;
  event_type: string;
  repository_id: string;
  number: number;
  status: string;
  attempts: number;
  error?: string | null;
  trigger_count: number;
  agent_queued_count: number;
  agent_preparing_count: number;
  agent_running_count: number;
  agent_completed_count: number;
  agent_failed_count: number;
  agent_timed_out_count: number;
  agent_cancelled_count: number;
  occurred_at: string;
  origin: "scanner" | "manual";
  source_activity_id?: string | null;
  source_activity_type?: string | null;
  source_occurred_at?: string | null;
  created_at: number;
  updated_at: number;
};
