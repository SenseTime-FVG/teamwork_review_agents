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
  enabled?: boolean;
  environment?: EnvironmentMap;
};

export type Agent = {
  prompt_file?: string;
  prompt?: string;
  model?: string;
  sandbox?: "read-only" | "workspace-write" | "danger-full-access";
  timeout_seconds?: number;
  write_scopes?: Array<"change_request" | "workspace">;
  allowed_sub_agents?: string[];
  output_schema?: string;
  skip_git_repo_check?: boolean;
  extra_codex_args?: string[];
  environment?: EnvironmentMap;
};

export type Rule = {
  name: string;
  events: string[];
  agents: string[];
  repositories?: string[];
  conditions?: Record<string, unknown>;
  enabled?: boolean;
};

export type ConfigDocument = {
  database: { path: string };
  scanner: Record<string, unknown>;
  runtime: Record<string, unknown>;
  web: Record<string, unknown>;
  environment: { global: EnvironmentMap };
  providers: Record<string, Record<string, unknown>>;
  repositories: Repository[];
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
  };
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
