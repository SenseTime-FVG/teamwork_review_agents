export type EnvironmentVariable = {
  value?: string;
  from_system?: string;
  secret?: boolean;
  expose_to_prompt?: boolean;
  expose_to_process?: boolean;
};

export type EnvironmentMap = Record<string, EnvironmentVariable | string | number | boolean | null>;

export type Repository = {
  id: string;
  provider: string;
  project: string;
  workspace: string;
  clone_url?: string;
  enabled?: boolean;
  environment?: EnvironmentMap;
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
  timeout_seconds?: number;
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

export type RuntimeConfig = Record<string, unknown> & {
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
    source: "runtime" | "user" | "builtin";
    label: string;
  };
  user_model?: string | null;
  user_config_path: string;
  catalog_error?: string | null;
  user_config_error?: string | null;
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
  config_revision: string;
  config_error?: string | null;
  last_started_at?: number | null;
  last_finished_at?: number | null;
  last_summary?: Record<string, unknown> | null;
  last_error?: string | null;
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
};

export type RunSummary = {
  run_id: string;
  root_run_id: string;
  parent_run_id?: string | null;
  event_id?: string | null;
  rule_name?: string | null;
  agent_name: string;
  resource_key: string;
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
  created_at: number;
  updated_at: number;
};
