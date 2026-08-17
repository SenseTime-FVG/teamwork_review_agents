"""分层环境变量解析、Prompt 模板渲染与 Secret 脱敏。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from .config import (
    AgentConfig,
    AppConfig,
    EnvironmentVariable,
    ProviderConfig,
    RepositoryConfig,
)
from .models import ChangeEvent


TEMPLATE_PATTERN = re.compile(r"\$\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}")
MASK = "********"


@dataclass(frozen=True)
class ResolvedEnvironment:
    """一次 Agent 运行最终使用的环境变量集合。"""

    all_values: dict[str, str]
    prompt_values: dict[str, str]
    process_values: dict[str, str]
    audit_values: dict[str, str]
    secret_values: tuple[str, ...]


def _resolve_variable(variable: EnvironmentVariable) -> str:
    """读取变量字面值或显式宿主机环境引用。"""

    if variable.from_system is not None:
        return os.getenv(variable.from_system, "")
    return variable.value or ""


def resolve_provider_token(config: AppConfig, provider: ProviderConfig) -> str:
    """优先从全局环境配置解析 Provider Token，再回退到宿主机环境。"""

    definition = config.environment.global_variables.get(provider.token_env)
    if definition is not None:
        return _resolve_variable(definition)
    return os.getenv(provider.token_env, "")


def runtime_variables(
    repository: RepositoryConfig,
    event: ChangeEvent,
    run_id: str,
    *,
    include_change_request: bool = True,
) -> dict[str, str]:
    """生成不可由用户覆盖的仓库、MR 和运行变量。"""

    variables = {
        "REPOSITORY_ID": repository.id,
        "REPOSITORY_PROJECT": repository.project,
        "REPOSITORY_WORKSPACE": str(repository.workspace),
        "RUN_ID": run_id,
    }
    if not include_change_request:
        return variables

    snapshot = event.current_snapshot
    variables.update({
        "MR_NUMBER": str(snapshot.number),
        "MR_TITLE": snapshot.title,
        "MR_STATE": snapshot.state,
        "MR_HEAD_SHA": snapshot.head_sha,
        "MR_SOURCE_BRANCH": snapshot.source_branch,
        "MR_TARGET_BRANCH": snapshot.target_branch,
        "MR_URL": snapshot.web_url,
        "EVENT_TYPE": event.type,
    })
    return variables


def resolve_environment(
    config: AppConfig,
    repository: RepositoryConfig,
    agent: AgentConfig,
    event: ChangeEvent,
    run_id: str,
    *,
    include_change_request: bool = True,
) -> ResolvedEnvironment:
    """按全局、仓库、Agent、运行变量顺序合并。"""

    definitions: dict[str, EnvironmentVariable] = {}
    definitions.update(config.environment.global_variables)
    definitions.update(repository.environment)
    definitions.update(agent.environment)
    provider_token_names = {
        provider.token_env for provider in config.providers.values()
    }

    all_values: dict[str, str] = {}
    prompt_values: dict[str, str] = {}
    process_values: dict[str, str] = {}
    audit_values: dict[str, str] = {}
    secrets: list[str] = []
    for name, definition in definitions.items():
        value = _resolve_variable(definition)
        is_provider_credential = name in provider_token_names
        is_secret = bool(definition.secret) or is_provider_credential
        all_values[name] = value
        prompt_values[name] = (
            value
            if definition.expose_to_prompt and not is_provider_credential
            else ""
        )
        if definition.expose_to_process and not is_provider_credential:
            process_values[name] = value
        audit_values[name] = MASK if is_secret and value else value
        if is_secret and value:
            secrets.append(value)

    builtins = runtime_variables(
        repository,
        event,
        run_id,
        include_change_request=include_change_request,
    )
    all_values.update(builtins)
    prompt_values.update(builtins)
    process_values.update(builtins)
    audit_values.update(builtins)
    return ResolvedEnvironment(
        all_values=all_values,
        prompt_values=prompt_values,
        process_values=process_values,
        audit_values=audit_values,
        secret_values=tuple(sorted(set(secrets), key=len, reverse=True)),
    )


def render_prompt(template: str, values: dict[str, str]) -> str:
    """渲染 `${{NAME}}`，缺失变量按要求替换为空字符串。"""

    return TEMPLATE_PATTERN.sub(lambda match: values.get(match.group(1), ""), template)


class SecretRedactor:
    """对文本和嵌套 JSON 数据执行确定性 Secret 替换。"""

    def __init__(self, secret_values: tuple[str, ...] | list[str]) -> None:
        self.secret_values = tuple(
            value for value in sorted(set(secret_values), key=len, reverse=True) if value
        )

    def text(self, value: str) -> str:
        """脱敏文本中的全部已知 Secret。"""

        redacted = value
        for secret in self.secret_values:
            redacted = redacted.replace(secret, MASK)
        return redacted

    def data(self, value: Any) -> Any:
        """递归脱敏可序列化对象。"""

        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.data(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.data(item) for item in value)
        if isinstance(value, dict):
            return {key: self.data(item) for key, item in value.items()}
        return value

    def json(self, value: Any) -> str:
        """脱敏后编码为 JSON。"""

        return json.dumps(self.data(value), ensure_ascii=False)
