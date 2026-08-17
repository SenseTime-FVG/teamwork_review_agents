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
from pydantic import BaseModel, Field, PositiveInt, model_validator


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


class RuntimeConfig(BaseModel):
    """Agent 运行、重试与资源锁配置。"""

    max_concurrent_agents: PositiveInt = 4
    lock_timeout_seconds: PositiveInt = 300
    lock_ttl_seconds: PositiveInt = 120
    max_sub_agent_depth: int = Field(default=2, ge=0)
    max_agent_runs_per_root: PositiveInt = 8
    event_retry_count: int = Field(default=2, ge=0)
    codex_binary: str = "codex"
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


class RepositoryConfig(BaseModel):
    """被扫描仓库及 Agent 本地工作目录配置。"""

    id: str
    provider: str
    project: str
    workspace: Path
    clone_url: str | None = None
    enabled: bool = True
    environment: dict[str, EnvironmentVariable] = Field(default_factory=dict)

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
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "read-only"
    timeout_seconds: PositiveInt = 1200
    write_scopes: list[Literal["change_request", "workspace"]] = Field(default_factory=list)
    allowed_sub_agents: list[str] = Field(default_factory=list)
    output_schema: Path | None = None
    skip_git_repo_check: bool = False
    extra_codex_args: list[str] = Field(default_factory=list)
    environment: dict[str, EnvironmentVariable] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_prompt_source(self) -> "AgentConfig":
        """要求 Agent 恰好配置一种 Prompt 来源。"""

        if bool(self.prompt_file) == bool(self.prompt):
            raise ValueError("Agent 必须且只能配置 prompt_file 或 prompt")
        return self


class RuleConfig(BaseModel):
    """事件到 Agent 的映射规则。"""

    name: str
    events: list[str]
    agents: list[str]
    repositories: list[str] | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)
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

        agent_names = set(self.agents)
        for agent_name, agent in self.agents.items():
            unknown = set(agent.allowed_sub_agents) - agent_names
            if unknown:
                raise ValueError(
                    f"Agent {agent_name} 引用了不存在的 sub-agent：{sorted(unknown)}"
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

    repositories: list[dict[str, Any]] = []
    for item in data.get("repositories", []):
        repository = dict(item)
        if "workspace" in repository:
            repository["workspace"] = _resolve_path(base_dir, repository["workspace"])
        repositories.append(repository)
    data["repositories"] = repositories

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
