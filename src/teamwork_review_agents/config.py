"""YAML 配置加载与跨引用校验。"""

from __future__ import annotations

import copy
import os
import hashlib
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, PositiveInt, field_validator, model_validator


CodexConfigPrimitive = str | int | float | bool
CodexConfigValue = CodexConfigPrimitive | list[CodexConfigPrimitive]

CODEX_STRUCTURED_CONFIG_KEYS = {
    "execution_mode",
    "model",
    "model_reasoning_effort",
    "model_verbosity",
    "personality",
    "web_search",
    "features.fast_mode",
    "service_tier",
}
CODEX_MANAGED_CONFIG_KEYS = {
    "approval_policy",
    "chatgpt_base_url",
    "hooks",
    "model_provider",
    "notify",
    "openai_base_url",
    "profile",
    "project_doc_max_bytes",
    "sandbox_mode",
    "sandbox_workspace_write",
    "shell_environment_policy",
    "features.network_proxy",
    "features.code_mode.direct_only_tool_namespaces",
}
CODEX_MANAGED_CONFIG_PREFIXES = (
    "agents.",
    "hooks.",
    "mcp_servers.",
    "skills.",
    "permissions.",
    "profiles.",
    "model_providers.",
    "otel.",
    "features.network_proxy.",
    "sandbox_workspace_write.",
    "shell_environment_policy.",
)

CODEX_SECURITY_CLI_OPTIONS = {
    "--ask-for-approval",
    "--cd",
    "--dangerously-bypass-approvals-and-sandbox",
    "--full-auto",
    "--permission-profile",
    "--profile",
    "--sandbox",
    "--yolo",
    "-C",
    "-a",
    "-p",
    "-s",
}
CODEX_SECURITY_CONFIG_KEYS = {
    "approval_policy",
    "default_permissions",
    "features.network_proxy",
    "profile",
    "sandbox_mode",
    "sandbox_workspace_write",
}
CODEX_SECURITY_CONFIG_PREFIXES = (
    "features.network_proxy.",
    "permissions.",
    "profiles.",
    "sandbox_workspace_write.",
)


class DatabaseConfig(BaseModel):
    """状态数据库配置。"""

    path: Path


class ScannerConfig(BaseModel):
    """轮询扫描配置。"""

    interval_seconds: PositiveInt = 300
    max_items_per_repository: PositiveInt = 100
    api_page_size: int = Field(default=50, ge=1, le=100)
    emit_initial_events: bool = False

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_pagination(cls, value: Any) -> Any:
        """兼容旧版 max_pages 与 page_size，并迁移为数量上限。"""

        if not isinstance(value, dict):
            return value
        data = dict(value)
        legacy_page_size = data.pop("page_size", None)
        legacy_max_pages = data.pop("max_pages", None)
        if "api_page_size" not in data and legacy_page_size is not None:
            data["api_page_size"] = legacy_page_size
        if (
            "max_items_per_repository" not in data
            and legacy_max_pages is not None
        ):
            page_size = legacy_page_size or data.get("api_page_size", 50)
            data["max_items_per_repository"] = int(legacy_max_pages) * int(page_size)
        return data


class CodexRuntimeConfig(BaseModel):
    """Teamwork 使用 Codex CLI 或内嵌模型客户端时的默认运行参数。"""

    execution_mode: Literal["cli", "model"] = "cli"
    model: str | None = None
    model_reasoning_effort: str | None = None
    fast_mode: Literal["inherit", "standard", "fast"] = "inherit"
    model_verbosity: Literal["low", "medium", "high"] | None = None
    personality: Literal["none", "friendly", "pragmatic"] | None = None
    web_search: Literal["disabled", "cached", "live"] | None = None
    extra_config: dict[str, CodexConfigValue] = Field(default_factory=dict)

    @field_validator("extra_config")
    @classmethod
    def validate_extra_config(
        cls,
        value: dict[str, CodexConfigValue],
    ) -> dict[str, CodexConfigValue]:
        """禁止高级项绕过结构化字段和应用托管的安全边界。"""

        invalid_format = sorted(
            key
            for key in value
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", key) is None
        )
        if invalid_format:
            raise ValueError(
                f"Codex 高级配置键格式无效：{invalid_format}"
            )
        protected = sorted(
            key
            for key in value
            if key in CODEX_STRUCTURED_CONFIG_KEYS
            or key in CODEX_MANAGED_CONFIG_KEYS
            or key.startswith(CODEX_MANAGED_CONFIG_PREFIXES)
        )
        if protected:
            raise ValueError(
                "Codex 高级配置不能覆盖结构化字段或应用托管配置："
                f"{protected}"
            )
        return value


class ManagedSandboxConfig(BaseModel):
    """Teamwork 托管的跨平台外层沙盒配置。"""

    enabled: bool = True
    fail_closed: bool = True


class RuntimeConfig(BaseModel):
    """Agent 运行、重试与资源锁配置。"""

    max_concurrent_agents: PositiveInt = 5
    agent_concurrency_limit: PositiveInt = 5
    lock_timeout_seconds: PositiveInt = 300
    lock_ttl_seconds: PositiveInt = 120
    max_sub_agent_depth: int = Field(default=2, ge=0)
    max_agent_runs_per_root: PositiveInt = 8
    event_retry_count: int = Field(default=2, ge=0)
    worktree_retention_days: PositiveInt = 7
    codex_binary: str = "codex"
    codex_home: Path | None = None
    expected_codex_version: str | None = None
    inherit_user_mcp_servers: bool = False
    allowed_user_mcp_servers: list[str] = Field(default_factory=list)
    repository_initialization_timeout_seconds: PositiveInt = 1800
    git_timeout_seconds: PositiveInt = 600
    agent_idle_timeout_seconds: PositiveInt = 300
    managed_sandbox: ManagedSandboxConfig = Field(
        default_factory=ManagedSandboxConfig,
    )
    codex: CodexRuntimeConfig = Field(default_factory=CodexRuntimeConfig)
    mcp_startup_timeout_seconds: PositiveInt = 15
    mcp_tool_timeout_seconds: PositiveInt = 1800
    max_jsonl_events: PositiveInt = 2000


class WebConfig(BaseModel):
    """后台管理 API 与 UI 配置。"""

    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    admin_token_env: str | None = None
    config_poll_seconds: PositiveInt = 2
    log_retention_days: PositiveInt = 30


class EnvironmentVariable(BaseModel):
    """一个可来自字面值或宿主机环境的变量。"""

    value: str | None = None
    from_system: str | None = None
    secret: bool = False
    expose_to_prompt: bool | None = None
    expose_to_process: bool = True

    @model_validator(mode="before")
    @classmethod
    def accept_shorthand(cls, value: Any) -> Any:
        """允许 YAML 中使用字符串、数字和布尔值简写。"""

        if isinstance(value, (str, int, float, bool)) or value is None:
            return {"value": "" if value is None else str(value)}
        return value

    @model_validator(mode="after")
    def validate_source(self) -> "EnvironmentVariable":
        """限制变量只能有一个来源，并设置安全的 Prompt 默认值。"""

        if self.value is not None and self.from_system is not None:
            raise ValueError("环境变量不能同时配置 value 和 from_system")
        if self.value is None and self.from_system is None:
            self.value = ""
        if self.expose_to_prompt is None:
            self.expose_to_prompt = not self.secret
        return self


class EnvironmentConfig(BaseModel):
    """全局环境变量容器。"""

    global_variables: dict[str, EnvironmentVariable] = Field(
        default_factory=dict,
        alias="global",
    )

    model_config = {"populate_by_name": True}


class ProviderConfig(BaseModel):
    """代码托管平台连接配置。"""

    kind: Literal["github", "gitlab"]
    base_url: str
    token_env: str
    request_timeout_seconds: PositiveInt = 30


class SkillConfig(BaseModel):
    """一个可由 Agent 独立装载的 Codex Skill 目录。"""

    path: Path


class PreflightStepConfig(BaseModel):
    """一个按参数数组执行的确定性 CI 步骤。"""

    name: str = Field(min_length=1)
    command: list[str] = Field(min_length=1)
    timeout_seconds: PositiveInt | None = None

    @field_validator("command")
    @classmethod
    def validate_command_program(cls, value: list[str]) -> list[str]:
        """拒绝缺少执行程序的空命令，避免把配置错误推迟到运行期。"""

        if not value[0].strip():
            raise ValueError("Preflight 步骤的执行程序不能为空")
        return value


class PreflightConfig(BaseModel):
    """仓库级 CI 能力与执行步骤配置。"""

    enabled: bool = False
    status_context: str = Field(default="teamwork/local-ci", min_length=1)
    timeout_seconds: PositiveInt = 1800
    max_output_bytes: PositiveInt = 1_000_000
    steps: list[PreflightStepConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_enabled_steps(self) -> "PreflightConfig":
        """启用门禁时至少需要一个可执行步骤。"""

        if self.enabled and not self.steps:
            raise ValueError("启用 Preflight 时必须配置至少一个步骤")
        return self


class RepositoryConfig(BaseModel):
    """被扫描仓库及 Agent 本地工作目录配置。"""

    id: str
    provider: str
    project: str
    workspace: Path
    clone_url: str | None = None
    enabled: bool = True
    environment: dict[str, EnvironmentVariable] = Field(default_factory=dict)
    preflight: PreflightConfig = Field(default_factory=PreflightConfig)

    @model_validator(mode="before")
    @classmethod
    def normalize_remote(cls, value: Any) -> Any:
        """保留可克隆地址，并把远端输入统一为平台项目路径。"""

        if not isinstance(value, dict):
            return value
        data = dict(value)
        project = str(data.get("project") or "").strip()
        is_remote_url = "://" in project or bool(
            re.match(r"^[^/@\s]+@[^:\s]+:", project)
        )
        if is_remote_url and not data.get("clone_url"):
            data["clone_url"] = project
        if "://" in project:
            project = urlparse(project).path
        elif re.match(r"^[^/@\s]+@[^:\s]+:", project):
            project = project.split(":", 1)[1]
        project = project.split("?", 1)[0].split("#", 1)[0].strip("/")
        if project.endswith(".git"):
            project = project[:-4]
        if not project or "/" not in project or any(
            segment in {"", ".", ".."} for segment in project.split("/")
        ):
            raise ValueError(
                "远端项目必须是 owner/repository、group/project、SSH 或 HTTPS 地址"
            )
        data["project"] = project
        return data


class AgentConfig(BaseModel):
    """一个可执行 Codex CLI Agent 的配置。"""

    prompt_file: Path | None = None
    prompt: str | None = None
    model: str | None = None
    model_reasoning_effort: str | None = None
    fast_mode: Literal["inherit", "standard", "fast"] = "inherit"
    model_verbosity: Literal["low", "medium", "high"] | None = None
    personality: Literal["none", "friendly", "pragmatic"] | None = None
    web_search: Literal["disabled", "cached", "live"] | None = None
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "read-only"
    home_mode: Literal["inherit", "temporary"] = "inherit"
    network_access: bool = False
    network_domains: list[str] = Field(default_factory=list)
    timeout_seconds: PositiveInt = 1200
    idle_timeout_seconds: PositiveInt | None = None
    max_concurrent_runs: PositiveInt | None = None
    write_scopes: list[Literal["change_request", "workspace"]] = Field(default_factory=list)
    allowed_sub_agents: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    output_schema: Path | None = None
    skip_git_repo_check: bool = False
    extra_codex_args: list[str] = Field(default_factory=list)
    environment: dict[str, EnvironmentVariable] = Field(default_factory=dict)

    @field_validator("network_domains")
    @classmethod
    def validate_network_domains(cls, value: list[str]) -> list[str]:
        """规范化命令联网域名，并拒绝 URL、端口和过宽通配符。"""

        domains: list[str] = []
        seen: set[str] = set()
        domain_pattern = re.compile(
            r"(?:(?:\*\*|\*)\.)?"
            r"(?=.{1,253}$)"
            r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        )
        for raw_domain in value:
            domain = raw_domain.strip().lower()
            if not domain or domain_pattern.fullmatch(domain) is None:
                raise ValueError(
                    "命令联网域名必须是主机名、*.example.com 或 **.example.com，"
                    f"不能包含协议、端口或路径：{raw_domain!r}"
                )
            if domain not in seen:
                seen.add(domain)
                domains.append(domain)
        return domains

    @field_validator("extra_codex_args")
    @classmethod
    def validate_extra_codex_args(cls, value: list[str]) -> list[str]:
        """禁止自定义 CLI 参数绕过 Teamwork 托管的执行边界。"""

        index = 0
        while index < len(value):
            argument = value[index]
            option = argument.split("=", 1)[0]
            attached_short_option = argument.startswith(("-C", "-a", "-p", "-s"))
            if option in CODEX_SECURITY_CLI_OPTIONS or (
                attached_short_option and not argument.startswith("--")
            ):
                raise ValueError(
                    "Agent extra_codex_args 不能覆盖沙盒、审批、权限档案或工作目录："
                    f"{argument}"
                )
            config_value: str | None = None
            if argument in {"--config", "-c"}:
                if index + 1 < len(value):
                    config_value = value[index + 1]
                index += 1
            elif argument.startswith("--config="):
                config_value = argument.removeprefix("--config=")
            if config_value and "=" in config_value:
                key = config_value.split("=", 1)[0].strip()
                if key in CODEX_SECURITY_CONFIG_KEYS or key.startswith(
                    CODEX_SECURITY_CONFIG_PREFIXES
                ):
                    raise ValueError(
                        "Agent extra_codex_args 不能覆盖 Teamwork 托管的安全配置："
                        f"{key}"
                    )
            index += 1
        return value

    @model_validator(mode="after")
    def validate_prompt_source(self) -> "AgentConfig":
        """要求 Agent 恰好配置一种 Prompt 来源。"""

        if bool(self.prompt_file) == bool(self.prompt):
            raise ValueError("Agent 必须且只能配置 prompt_file 或 prompt")
        return self

    @model_validator(mode="after")
    def validate_network_access(self) -> "AgentConfig":
        """校验命令联网、临时 HOME 与本地沙箱的可执行组合。"""

        if self.sandbox == "read-only" and self.home_mode == "temporary":
            raise ValueError("read-only 沙箱不能配置可写的临时 HOME")
        if self.network_domains and not self.network_access:
            raise ValueError("配置命令联网域名白名单前必须先允许命令联网")
        if self.sandbox == "read-only" and self.network_access:
            raise ValueError("read-only 沙箱不能通过 Agent 配置允许命令联网")
        if self.sandbox == "danger-full-access":
            if self.network_domains:
                raise ValueError(
                    "danger-full-access 的网络不受域名白名单保护，"
                    "不能配置 network_domains"
                )
            # 完全访问沙箱本身不隔离网络，统一记录为真实的有效状态。
            self.network_access = True
        return self


class RuleConfig(BaseModel):
    """事件到 Agent 的映射规则。"""

    name: str
    events: list[str]
    agents: list[str]
    repositories: list[str] | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)
    deduplicate_per_scan: bool = False
    inherit_workspace: bool = False
    run_preflight: bool = False
    enabled: bool = True


class AppConfig(BaseModel):
    """应用完整配置。"""

    database: DatabaseConfig
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    repositories: list[RepositoryConfig] = Field(default_factory=list)
    skills: dict[str, SkillConfig] = Field(default_factory=dict)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    rules: list[RuleConfig] = Field(default_factory=list)
    config_path: Path = Field(exclude=True)
    revision: str = Field(exclude=True)

    @model_validator(mode="after")
    def validate_references(self) -> "AppConfig":
        """检查配置中的名称引用，尽早阻止运行期错误。"""

        repository_ids = [repository.id for repository in self.repositories]
        if len(repository_ids) != len(set(repository_ids)):
            raise ValueError("repositories 中存在重复 id")

        reserved_token_names = {"CODEX_API_KEY", "OPENAI_API_KEY"}
        provider_token_names = {
            provider.token_env for provider in self.providers.values()
        }
        for provider_name, provider in self.providers.items():
            if provider.token_env in reserved_token_names:
                raise ValueError(
                    f"Provider {provider_name} 的 token_env 不能复用 Codex 凭据变量："
                    f"{provider.token_env}"
                )

        # Provider 凭据属于扫描器，不允许被 Prompt 或 Codex 子进程继承。
        environment_maps = [self.environment.global_variables]
        environment_maps.extend(
            repository.environment for repository in self.repositories
        )
        environment_maps.extend(agent.environment for agent in self.agents.values())
        for environment_map in environment_maps:
            for name in provider_token_names & environment_map.keys():
                definition = environment_map[name]
                definition.secret = True
                definition.expose_to_prompt = False
                definition.expose_to_process = False

        environment_names = set(self.environment.global_variables)
        for repository in self.repositories:
            environment_names.update(repository.environment)
        for agent in self.agents.values():
            environment_names.update(agent.environment)
        invalid_environment_names = sorted(
            name
            for name in environment_names
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) is None
        )
        if invalid_environment_names:
            raise ValueError(
                f"环境变量名只能包含字母、数字和下划线，且必须以字母开头："
                f"{invalid_environment_names}"
            )

        for repository in self.repositories:
            if repository.provider not in self.providers:
                raise ValueError(
                    f"仓库 {repository.id} 引用了不存在的 Provider：{repository.provider}"
                )
            if (
                repository.preflight.enabled
                and self.providers[repository.provider].kind != "github"
            ):
                raise ValueError(
                    f"仓库 {repository.id} 启用了 Preflight，第一版仅支持 GitHub"
                )

        agent_names = set(self.agents)
        skill_names = set(self.skills)
        skill_metadata_names: dict[str, str] = {}
        from .skill_files import read_skill_metadata

        for skill_id, skill in self.skills.items():
            try:
                metadata = read_skill_metadata(skill.path)
            except ValueError as exc:
                raise ValueError(f"Skill {skill_id} 无效：{exc}") from exc
            previous = skill_metadata_names.get(metadata.name)
            if previous is not None:
                raise ValueError(
                    f"Skill {skill_id} 与 {previous} 的 SKILL.md name 重复："
                    f"{metadata.name}"
                )
            skill_metadata_names[metadata.name] = skill_id
        for agent_name, agent in self.agents.items():
            unknown = set(agent.allowed_sub_agents) - agent_names
            if unknown:
                raise ValueError(
                    f"Agent {agent_name} 引用了不存在的 sub-agent：{sorted(unknown)}"
                )
            unknown_skills = set(agent.skills) - skill_names
            if unknown_skills:
                raise ValueError(
                    f"Agent {agent_name} 引用了不存在的 Skill：{sorted(unknown_skills)}"
                )
            if "workspace" in agent.write_scopes and agent.sandbox == "read-only":
                raise ValueError(
                    f"Agent {agent_name} 声明 workspace 写作用域，但 sandbox 是 read-only"
                )
            if agent.sandbox != "read-only" and "workspace" not in agent.write_scopes:
                raise ValueError(
                    f"Agent {agent_name} 使用可写 sandbox，但没有声明 workspace 写作用域"
                )

        known_repositories = set(repository_ids)
        rule_names: set[str] = set()
        for rule in self.rules:
            if rule.name in rule_names:
                raise ValueError(f"存在重复规则名：{rule.name}")
            rule_names.add(rule.name)
            unknown_agents = set(rule.agents) - agent_names
            if unknown_agents:
                raise ValueError(
                    f"规则 {rule.name} 引用了不存在的 Agent：{sorted(unknown_agents)}"
                )
            if rule.repositories:
                unknown_repositories = set(rule.repositories) - known_repositories
                if unknown_repositories:
                    raise ValueError(
                        f"规则 {rule.name} 引用了不存在的仓库：{sorted(unknown_repositories)}"
                    )
        return self

    def repository_map(self) -> dict[str, RepositoryConfig]:
        """按仓库 id 返回索引。"""

        return {repository.id: repository for repository in self.repositories}


def _resolve_path(base_dir: Path, value: str | Path) -> Path:
    """展开环境变量和用户目录，并以配置目录解析相对路径。"""

    expanded = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if not expanded.is_absolute():
        expanded = base_dir / expanded
    return expanded.resolve()


def protect_provider_credentials(raw: dict[str, Any]) -> dict[str, Any]:
    """复制配置并强制隔离所有与 Provider Token 同名的变量。"""

    data = copy.deepcopy(raw)
    providers = data.get("providers", {})
    if not isinstance(providers, dict):
        return data
    token_names = {
        str(provider.get("token_env"))
        for provider in providers.values()
        if isinstance(provider, dict) and provider.get("token_env")
    }
    if not token_names:
        return data

    environment_maps: list[Any] = []
    environment = data.get("environment", {})
    if isinstance(environment, dict):
        environment_maps.append(environment.get("global", {}))
    repositories = data.get("repositories", [])
    if isinstance(repositories, list):
        environment_maps.extend(
            repository.get("environment", {})
            for repository in repositories
            if isinstance(repository, dict)
        )
    agents = data.get("agents", {})
    if isinstance(agents, dict):
        environment_maps.extend(
            agent.get("environment", {})
            for agent in agents.values()
            if isinstance(agent, dict)
        )

    for environment_map in environment_maps:
        if not isinstance(environment_map, dict):
            continue
        for name in token_names & environment_map.keys():
            raw_definition = environment_map[name]
            if isinstance(raw_definition, dict):
                definition = dict(raw_definition)
            elif isinstance(raw_definition, (str, int, float, bool)) or raw_definition is None:
                definition = {
                    "value": "" if raw_definition is None else str(raw_definition)
                }
            else:
                # 非法结构留给 Pydantic 给出原始校验错误。
                continue
            definition["secret"] = True
            definition["expose_to_prompt"] = False
            definition["expose_to_process"] = False
            environment_map[name] = definition
    return data


def _resolve_config_paths(raw: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """原地复制并规范化所有文件系统路径。"""

    data = dict(raw)
    data["database"] = dict(data.get("database", {}))
    if "path" in data["database"]:
        data["database"]["path"] = _resolve_path(base_dir, data["database"]["path"])

    data["runtime"] = dict(data.get("runtime", {}))
    if data["runtime"].get("codex_home"):
        data["runtime"]["codex_home"] = _resolve_path(
            base_dir,
            data["runtime"]["codex_home"],
        )

    repositories: list[dict[str, Any]] = []
    for item in data.get("repositories", []):
        repository = dict(item)
        if "workspace" in repository:
            repository["workspace"] = _resolve_path(base_dir, repository["workspace"])
        repositories.append(repository)
    data["repositories"] = repositories

    skills: dict[str, dict[str, Any]] = {}
    for name, item in data.get("skills", {}).items():
        skill = dict(item)
        if skill.get("path"):
            skill["path"] = _resolve_path(base_dir, skill["path"])
        skills[name] = skill
    data["skills"] = skills

    agents: dict[str, dict[str, Any]] = {}
    for name, item in data.get("agents", {}).items():
        agent = dict(item)
        if agent.get("prompt_file"):
            agent["prompt_file"] = _resolve_path(base_dir, agent["prompt_file"])
        if agent.get("output_schema"):
            agent["output_schema"] = _resolve_path(base_dir, agent["output_schema"])
        agents[name] = agent
    data["agents"] = agents
    return data


def parse_config_data(raw: dict[str, Any], config_path: str | Path) -> AppConfig:
    """解析已经读取的配置数据，供文件加载和 UI 保存前校验复用。"""

    resolved_path = Path(config_path).expanduser().resolve()
    if not isinstance(raw, dict):
        raise ValueError("配置文件顶层必须是对象")
    protected = protect_provider_credentials(raw)
    resolved = _resolve_config_paths(protected, resolved_path.parent)
    serialized = yaml.safe_dump(protected, allow_unicode=True, sort_keys=False)
    resolved["config_path"] = resolved_path
    resolved["revision"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return AppConfig.model_validate(resolved)


def load_config(path: str | Path) -> AppConfig:
    """从 YAML 文件加载、解析并校验应用配置。"""

    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}
    return parse_config_data(raw, config_path)


def validate_runtime_files(config: AppConfig) -> list[str]:
    """返回提示词、工作目录和可选 Schema 的文件问题。"""

    errors: list[str] = []
    skill_metadata_names: dict[str, str] = {}
    from .skill_files import read_skill_metadata

    for skill_id, skill in config.skills.items():
        try:
            metadata = read_skill_metadata(skill.path)
        except ValueError as exc:
            errors.append(f"Skill {skill_id} 无效：{exc}")
            continue
        previous = skill_metadata_names.get(metadata.name)
        if previous is not None:
            errors.append(
                f"Skill {skill_id} 与 {previous} 的 SKILL.md name 重复：{metadata.name}"
            )
        else:
            skill_metadata_names[metadata.name] = skill_id
    for name, agent in config.agents.items():
        if agent.prompt_file and not agent.prompt_file.is_file():
            errors.append(f"Agent {name} 的提示词文件不存在：{agent.prompt_file}")
        if agent.output_schema and not agent.output_schema.is_file():
            errors.append(f"Agent {name} 的输出 Schema 不存在：{agent.output_schema}")
    for repository in config.repositories:
        if repository.enabled and repository.workspace.exists():
            if not repository.workspace.is_dir():
                errors.append(
                    f"仓库 {repository.id} 的本地工作目录不是文件夹："
                    f"{repository.workspace}"
                )
            elif not (repository.workspace / ".git").exists():
                errors.append(
                    f"仓库 {repository.id} 的本地工作目录已存在但不是 Git 仓库："
                    f"{repository.workspace}"
                )
    if config.web.admin_token_env and not os.getenv(config.web.admin_token_env):
        errors.append(
            f"后台管理员 Token 环境变量不存在：{config.web.admin_token_env}"
        )
    if config.web.host not in {"127.0.0.1", "localhost", "::1"} and not config.web.admin_token_env:
        errors.append("后台监听非本机地址时必须配置 web.admin_token_env")
    return errors
