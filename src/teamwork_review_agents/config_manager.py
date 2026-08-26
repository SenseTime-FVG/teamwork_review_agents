"""YAML 配置热加载、原子保存、Secret 占位合并和版本记录。"""

from __future__ import annotations

import copy
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml

from .config import (
    AppConfig,
    BUILTIN_CODEX_CLI_PROVIDER_ID,
    load_config,
    normalize_model_provider_document,
    parse_config_data,
    protect_provider_credentials,
)
from .environment import MASK
from .state import StateStore


class ConfigRevisionConflict(ValueError):
    """配置版本已经变化，当前局部修改不能安全合并。"""


class ConfigManager:
    """维护最后一版有效配置，并允许 UI 与手工编辑安全共存。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()
        self._config = load_config(self.path)
        self._mtime_ns = self.path.stat().st_mtime_ns
        self.last_error: str | None = None
        self.store = StateStore(self._config.database.path)
        self.store.initialize()
        self._record_version(self._config, "startup")

    @property
    def config(self) -> AppConfig:
        """返回当前最后一版有效配置。"""

        with self._lock:
            return self._config

    def _read_raw(self) -> dict[str, Any]:
        """读取 YAML 原始结构并保持相对路径。"""

        with self.path.open("r", encoding="utf-8") as file:
            raw = yaml.safe_load(file) or {}
        if not isinstance(raw, dict):
            raise ValueError("配置文件顶层必须是对象")
        return normalize_model_provider_document(raw)

    @staticmethod
    def _mask_secrets(value: Any) -> Any:
        """递归隐藏环境变量对象中的字面 Secret。"""

        if isinstance(value, list):
            return [ConfigManager._mask_secrets(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {
            key: ConfigManager._mask_secrets(item)
            for key, item in value.items()
        }
        if result.get("secret") is True and "value" in result and result["value"]:
            result["value"] = MASK
        return result

    @staticmethod
    def _merge_masked(incoming: Any, current: Any) -> Any:
        """将 UI 未修改的 Secret 占位符还原为当前值。"""

        if incoming == MASK:
            return copy.deepcopy(current)
        if isinstance(incoming, list) and isinstance(current, list):
            # 仓库、规则等有稳定标识的列表按标识合并，避免 UI 重排后 Secret 串位。
            for identity in ("id", "name"):
                if (
                    all(isinstance(item, dict) and identity in item for item in incoming)
                    and all(isinstance(item, dict) and identity in item for item in current)
                ):
                    current_by_identity = {item[identity]: item for item in current}
                    return [
                        ConfigManager._merge_masked(
                            item,
                            current_by_identity.get(item[identity]),
                        )
                        for item in incoming
                    ]
            return [
                ConfigManager._merge_masked(item, current[index] if index < len(current) else None)
                for index, item in enumerate(incoming)
            ]
        if isinstance(incoming, dict) and isinstance(current, dict):
            return {
                key: ConfigManager._merge_masked(item, current.get(key))
                for key, item in incoming.items()
            }
        return copy.deepcopy(incoming)

    def document(self, *, mask_secrets: bool = True) -> dict[str, Any]:
        """返回 UI 使用的原始配置结构。"""

        with self._lock:
            raw = protect_provider_credentials(self._read_raw())
        return self._mask_secrets(raw) if mask_secrets else raw

    def _record_version(self, config: AppConfig, source: str) -> None:
        """只保存脱敏后的配置历史。"""

        protected = protect_provider_credentials(self._read_raw())
        masked = self._mask_secrets(protected)
        content = yaml.safe_dump(masked, allow_unicode=True, sort_keys=False)
        self.store.save_config_version(config.revision, content, source)

    def validate(self, document: dict[str, Any]) -> AppConfig:
        """合并 Secret 占位并执行完整配置校验。"""

        current = protect_provider_credentials(self._read_raw())
        incoming = protect_provider_credentials(document)
        merged = protect_provider_credentials(self._merge_masked(incoming, current))
        return parse_config_data(merged, self.path)

    def save(self, document: dict[str, Any], *, source: str = "ui") -> AppConfig:
        """校验后在同目录原子替换 YAML，并更新有效配置。"""

        with self._lock:
            current_raw = protect_provider_credentials(self._read_raw())
            incoming = protect_provider_credentials(document)
            merged = protect_provider_credentials(
                self._merge_masked(incoming, current_raw)
            )
            return self._persist_locked(merged, source=source)

    def save_agent(
        self,
        *,
        expected_revision: str,
        name: str,
        agent: dict[str, Any],
        original_name: str | None = None,
        source: str = "ui-agent",
    ) -> AppConfig:
        """基于最新配置原子创建或更新一个 Agent。"""

        with self._lock:
            current_raw = protect_provider_credentials(self._read_raw())
            self._assert_revision(current_raw, expected_revision)
            next_name = name.strip()
            if not next_name:
                raise ValueError("Agent 名称不能为空")
            agents = current_raw.get("agents", {})
            if not isinstance(agents, dict):
                raise ValueError("agents 配置必须是对象")
            current_name = original_name.strip() if original_name is not None else None
            if current_name is None:
                if next_name in agents:
                    raise ValueError(f"Agent 已存在：{next_name}")
            else:
                if current_name not in agents:
                    raise ValueError(f"Agent 不存在：{current_name}")
                if next_name != current_name and next_name in agents:
                    raise ValueError(f"Agent 已存在：{next_name}")

            document = copy.deepcopy(current_raw)
            next_agents: dict[str, Any] = {}
            for agent_name, value in agents.items():
                if current_name is not None and agent_name == current_name:
                    next_agents[next_name] = copy.deepcopy(agent)
                    continue
                next_agents[agent_name] = copy.deepcopy(value)
            if current_name is None:
                next_agents[next_name] = copy.deepcopy(agent)

            if current_name is not None and current_name != next_name:
                for value in next_agents.values():
                    if not isinstance(value, dict):
                        continue
                    allowed = value.get("allowed_sub_agents")
                    if isinstance(allowed, list):
                        value["allowed_sub_agents"] = [
                            next_name if item == current_name else item
                            for item in allowed
                        ]
                for rule in document.get("rules", []):
                    if (
                        not isinstance(rule, dict)
                        or not isinstance(rule.get("agents"), list)
                    ):
                        continue
                    rule["agents"] = [
                        next_name if item == current_name else item
                        for item in rule["agents"]
                    ]
            document["agents"] = next_agents
            merged = protect_provider_credentials(
                self._merge_masked(document, current_raw)
            )
            return self._persist_locked(merged, source=source)

    def delete_agent(
        self,
        *,
        expected_revision: str,
        name: str,
        source: str = "ui-agent-delete",
    ) -> AppConfig:
        """删除一个 Agent，并同步清理规则和 sub-agent 引用。"""

        with self._lock:
            current_raw = protect_provider_credentials(self._read_raw())
            self._assert_revision(current_raw, expected_revision)
            agents = current_raw.get("agents", {})
            if not isinstance(agents, dict) or name not in agents:
                raise ValueError(f"Agent 不存在：{name}")

            document = copy.deepcopy(current_raw)
            next_agents = {
                agent_name: copy.deepcopy(value)
                for agent_name, value in agents.items()
                if agent_name != name
            }
            for value in next_agents.values():
                if not isinstance(value, dict):
                    continue
                allowed = value.get("allowed_sub_agents")
                if isinstance(allowed, list):
                    value["allowed_sub_agents"] = [
                        item for item in allowed if item != name
                    ]
            for rule in document.get("rules", []):
                if (
                    not isinstance(rule, dict)
                    or not isinstance(rule.get("agents"), list)
                ):
                    continue
                rule["agents"] = [item for item in rule["agents"] if item != name]
            document["agents"] = next_agents
            return self._persist_locked(document, source=source)

    def save_rule(
        self,
        *,
        expected_revision: str,
        name: str,
        rule: dict[str, Any],
        original_name: str | None = None,
        source: str = "ui-rule",
    ) -> AppConfig:
        """基于最新配置原子创建或更新一条触发规则。"""

        with self._lock:
            current_raw = protect_provider_credentials(self._read_raw())
            self._assert_revision(current_raw, expected_revision)
            next_name = name.strip()
            if not next_name:
                raise ValueError("规则名称不能为空")
            rules = current_raw.get("rules", [])
            if not isinstance(rules, list):
                raise ValueError("rules 配置必须是数组")
            if not all(isinstance(item, dict) for item in rules):
                raise ValueError("rules 中的每一项都必须是对象")

            current_name = original_name.strip() if original_name is not None else None
            rule_names = [str(item.get("name", "")) for item in rules]
            if current_name is None:
                if next_name in rule_names:
                    raise ValueError(f"规则已存在：{next_name}")
                target_index = len(rules)
            else:
                if current_name not in rule_names:
                    raise ValueError(f"规则不存在：{current_name}")
                if next_name != current_name and next_name in rule_names:
                    raise ValueError(f"规则已存在：{next_name}")
                target_index = rule_names.index(current_name)

            next_rule = copy.deepcopy(rule)
            next_rule["name"] = next_name
            next_rules = [copy.deepcopy(item) for item in rules]
            if current_name is None:
                next_rules.append(next_rule)
            else:
                # 保留原数组位置，避免修改单条规则时改变匹配顺序。
                next_rules[target_index] = next_rule

            document = copy.deepcopy(current_raw)
            document["rules"] = next_rules
            merged = protect_provider_credentials(
                self._merge_masked(document, current_raw)
            )
            return self._persist_locked(merged, source=source)

    def delete_rule(
        self,
        *,
        expected_revision: str,
        name: str,
        source: str = "ui-rule-delete",
    ) -> AppConfig:
        """基于最新配置原子删除一条触发规则。"""

        with self._lock:
            current_raw = protect_provider_credentials(self._read_raw())
            self._assert_revision(current_raw, expected_revision)
            rules = current_raw.get("rules", [])
            if not isinstance(rules, list):
                raise ValueError("rules 配置必须是数组")
            if not any(
                isinstance(item, dict) and item.get("name") == name
                for item in rules
            ):
                raise ValueError(f"规则不存在：{name}")

            document = copy.deepcopy(current_raw)
            document["rules"] = [
                copy.deepcopy(item)
                for item in rules
                if not (isinstance(item, dict) and item.get("name") == name)
            ]
            return self._persist_locked(document, source=source)

    def save_provider(
        self,
        *,
        expected_revision: str,
        name: str,
        provider: dict[str, Any],
        original_name: str | None = None,
        source: str = "ui-provider",
    ) -> AppConfig:
        """基于最新配置原子创建或更新一个平台连接。"""

        with self._lock:
            current_raw = protect_provider_credentials(self._read_raw())
            self._assert_revision(current_raw, expected_revision)
            next_name = name.strip()
            if not next_name:
                raise ValueError("平台连接名称不能为空")
            providers = current_raw.get("providers", {})
            if not isinstance(providers, dict):
                raise ValueError("providers 配置必须是对象")
            if not all(isinstance(item, dict) for item in providers.values()):
                raise ValueError("providers 中的每一项都必须是对象")

            current_name = original_name.strip() if original_name is not None else None
            if current_name is None:
                if next_name in providers:
                    raise ValueError(f"平台连接已存在：{next_name}")
            else:
                if current_name not in providers:
                    raise ValueError(f"平台连接不存在：{current_name}")
                if next_name != current_name and next_name in providers:
                    raise ValueError(f"平台连接已存在：{next_name}")

            next_providers: dict[str, Any] = {}
            for provider_name, value in providers.items():
                if current_name is not None and provider_name == current_name:
                    next_providers[next_name] = copy.deepcopy(provider)
                    continue
                next_providers[provider_name] = copy.deepcopy(value)
            if current_name is None:
                next_providers[next_name] = copy.deepcopy(provider)

            document = copy.deepcopy(current_raw)
            document["providers"] = next_providers
            if current_name is not None and current_name != next_name:
                for repository in document.get("repositories", []):
                    if (
                        isinstance(repository, dict)
                        and repository.get("provider") == current_name
                    ):
                        repository["provider"] = next_name
            merged = protect_provider_credentials(
                self._merge_masked(document, current_raw)
            )
            return self._persist_locked(merged, source=source)

    def delete_provider(
        self,
        *,
        expected_revision: str,
        name: str,
        source: str = "ui-provider-delete",
    ) -> AppConfig:
        """删除未被仓库引用的平台连接。"""

        with self._lock:
            current_raw = protect_provider_credentials(self._read_raw())
            self._assert_revision(current_raw, expected_revision)
            providers = current_raw.get("providers", {})
            if not isinstance(providers, dict) or name not in providers:
                raise ValueError(f"平台连接不存在：{name}")
            referenced_repositories = [
                str(repository.get("id", ""))
                for repository in current_raw.get("repositories", [])
                if isinstance(repository, dict)
                and repository.get("provider") == name
            ]
            if referenced_repositories:
                raise ValueError(
                    f"平台连接 {name} 仍被仓库引用：{referenced_repositories}"
                )

            document = copy.deepcopy(current_raw)
            document["providers"] = {
                provider_name: copy.deepcopy(value)
                for provider_name, value in providers.items()
                if provider_name != name
            }
            return self._persist_locked(document, source=source)

    def save_model_provider(
        self,
        *,
        expected_revision: str,
        provider_id: str,
        provider: dict[str, Any],
        codex_runtime: dict[str, Any] | None = None,
        source: str = "ui-model-provider",
    ) -> AppConfig:
        """基于最新配置创建或更新一个模型 Provider。"""

        with self._lock:
            current_raw = protect_provider_credentials(self._read_raw())
            self._assert_revision(current_raw, expected_revision)
            normalized_id = provider_id.strip()
            if not normalized_id:
                raise ValueError("模型 Provider ID 不能为空")
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", normalized_id) is None:
                raise ValueError(
                    "模型 Provider ID 只能包含字母、数字、点、下划线和短横线，"
                    "且必须以字母开头"
                )
            model_providers = current_raw.get("model_providers", {})
            if not isinstance(model_providers, dict):
                raise ValueError("model_providers 配置必须是对象")
            next_provider = copy.deepcopy(provider)
            if normalized_id == BUILTIN_CODEX_CLI_PROVIDER_ID:
                existing = model_providers.get(normalized_id)
                if not isinstance(existing, dict):
                    raise ValueError("内置 Codex CLI Provider 不存在")
                if next_provider.get("driver") != "codex_cli":
                    raise ValueError("内置 Codex CLI Provider 不允许改变驱动类型")
            elif codex_runtime is not None:
                raise ValueError("只有内置 Codex CLI Provider 可以更新 Codex 运行参数")

            document = copy.deepcopy(current_raw)
            document["model_providers"] = {
                **copy.deepcopy(model_providers),
                normalized_id: next_provider,
            }
            if codex_runtime is not None:
                runtime = document.setdefault("runtime", {})
                if not isinstance(runtime, dict):
                    raise ValueError("runtime 配置必须是对象")
                allowed_keys = {
                    "codex_binary",
                    "codex_home",
                    "expected_codex_version",
                    "inherit_user_mcp_servers",
                    "allowed_user_mcp_servers",
                    "codex",
                }
                unknown_keys = sorted(set(codex_runtime) - allowed_keys)
                if unknown_keys:
                    raise ValueError(f"Codex Provider 运行参数包含未知字段：{unknown_keys}")
                for key, value in codex_runtime.items():
                    if value is None:
                        runtime.pop(key, None)
                    else:
                        runtime[key] = copy.deepcopy(value)
            return self._persist_locked(document, source=source)

    def delete_model_provider(
        self,
        *,
        expected_revision: str,
        provider_id: str,
        source: str = "ui-model-provider-delete",
    ) -> AppConfig:
        """删除外部模型 Provider，并原子迁移默认模型和 Agent 引用。"""

        with self._lock:
            current_raw = protect_provider_credentials(self._read_raw())
            self._assert_revision(current_raw, expected_revision)
            normalized_id = provider_id.strip()
            if normalized_id == BUILTIN_CODEX_CLI_PROVIDER_ID:
                raise ValueError("内置 Codex CLI Provider 不允许删除")
            model_providers = current_raw.get("model_providers", {})
            if not isinstance(model_providers, dict) or normalized_id not in model_providers:
                raise ValueError(f"模型 Provider 不存在：{normalized_id}")

            document = copy.deepcopy(current_raw)
            document["model_providers"] = {
                key: copy.deepcopy(value)
                for key, value in model_providers.items()
                if key != normalized_id
            }
            runtime = document.setdefault("runtime", {})
            default_model = runtime.get("default_model") if isinstance(runtime, dict) else None
            if (
                isinstance(default_model, dict)
                and default_model.get("provider") == normalized_id
            ):
                runtime["default_model"] = {
                    "provider": BUILTIN_CODEX_CLI_PROVIDER_ID,
                }
            agents = document.get("agents", {})
            if isinstance(agents, dict):
                for agent in agents.values():
                    if not isinstance(agent, dict):
                        continue
                    if agent.get("model_provider") == normalized_id:
                        agent.pop("model_provider", None)
                        agent.pop("model", None)
                    fallbacks = agent.get("model_fallbacks")
                    if isinstance(fallbacks, list):
                        agent["model_fallbacks"] = [
                            item
                            for item in fallbacks
                            if not (
                                isinstance(item, dict)
                                and item.get("provider") == normalized_id
                            )
                        ]
            if isinstance(runtime, dict):
                fallbacks = runtime.get("default_model_fallbacks")
                if isinstance(fallbacks, list):
                    runtime["default_model_fallbacks"] = [
                        item
                        for item in fallbacks
                        if not (
                            isinstance(item, dict)
                            and item.get("provider") == normalized_id
                        )
                    ]
            return self._persist_locked(document, source=source)

    def save_repository(
        self,
        *,
        expected_revision: str,
        repository_id: str,
        repository: dict[str, Any],
        original_id: str | None = None,
        source: str = "ui-repository",
    ) -> AppConfig:
        """基于最新配置原子创建或更新一个仓库。"""

        with self._lock:
            current_raw = protect_provider_credentials(self._read_raw())
            self._assert_revision(current_raw, expected_revision)
            next_id = repository_id.strip()
            if not next_id:
                raise ValueError("仓库 ID 不能为空")
            repositories = current_raw.get("repositories", [])
            if not isinstance(repositories, list):
                raise ValueError("repositories 配置必须是数组")
            if not all(isinstance(item, dict) for item in repositories):
                raise ValueError("repositories 中的每一项都必须是对象")

            current_id = original_id.strip() if original_id is not None else None
            repository_ids = [str(item.get("id", "")) for item in repositories]
            if current_id is None:
                if next_id in repository_ids:
                    raise ValueError(f"仓库已存在：{next_id}")
                target_index = len(repositories)
            else:
                if current_id not in repository_ids:
                    raise ValueError(f"仓库不存在：{current_id}")
                if next_id != current_id:
                    raise ValueError("已有仓库的 ID 不允许修改，请新建仓库")
                target_index = repository_ids.index(current_id)

            next_repository = copy.deepcopy(repository)
            next_repository["id"] = next_id
            next_repositories = [copy.deepcopy(item) for item in repositories]
            if current_id is None:
                next_repositories.append(next_repository)
            else:
                # 保留仓库数组位置，避免局部保存造成无意义的配置重排。
                next_repositories[target_index] = next_repository

            document = copy.deepcopy(current_raw)
            document["repositories"] = next_repositories
            merged = protect_provider_credentials(
                self._merge_masked(document, current_raw)
            )
            return self._persist_locked(merged, source=source)

    def delete_repository(
        self,
        *,
        expected_revision: str,
        repository_id: str,
        source: str = "ui-repository-delete",
    ) -> AppConfig:
        """删除未被触发规则显式引用的仓库配置。"""

        with self._lock:
            current_raw = protect_provider_credentials(self._read_raw())
            self._assert_revision(current_raw, expected_revision)
            repositories = current_raw.get("repositories", [])
            if not isinstance(repositories, list) or not any(
                isinstance(item, dict) and item.get("id") == repository_id
                for item in repositories
            ):
                raise ValueError(f"仓库不存在：{repository_id}")
            referencing_rules = [
                str(rule.get("name", ""))
                for rule in current_raw.get("rules", [])
                if isinstance(rule, dict)
                and isinstance(rule.get("repositories"), list)
                and repository_id in rule["repositories"]
            ]
            if referencing_rules:
                raise ValueError(
                    f"仓库 {repository_id} 仍被触发规则引用：{referencing_rules}"
                )

            document = copy.deepcopy(current_raw)
            document["repositories"] = [
                copy.deepcopy(item)
                for item in repositories
                if not (isinstance(item, dict) and item.get("id") == repository_id)
            ]
            return self._persist_locked(document, source=source)

    def _assert_revision(
        self,
        current_raw: dict[str, Any],
        expected_revision: str,
    ) -> None:
        """确认局部保存仍基于管理员打开详情时的配置版本。"""

        current_revision = parse_config_data(current_raw, self.path).revision
        if current_revision != expected_revision:
            raise ConfigRevisionConflict(
                "配置已经被其他操作更新，请重新加载后再编辑"
            )

    def _persist_locked(
        self,
        document: dict[str, Any],
        *,
        source: str,
    ) -> AppConfig:
        """在已经持有配置锁时校验并原子写入配置。"""

        config = parse_config_data(document, self.path)
        if config.database.path != self._config.database.path:
            raise ValueError("后台运行期间不允许通过 UI 修改 database.path")
        content = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = file.name
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)
        self._config = config
        self._mtime_ns = self.path.stat().st_mtime_ns
        self.last_error = None
        self._record_version(config, source)
        return config

    def reload_if_changed(self) -> bool:
        """检测手工编辑；无效时保留上一版配置。"""

        with self._lock:
            mtime_ns = self.path.stat().st_mtime_ns
            if mtime_ns == self._mtime_ns:
                return False
            self._mtime_ns = mtime_ns
            try:
                config = load_config(self.path)
                if config.database.path != self._config.database.path:
                    raise ValueError("运行期间不允许热切换 database.path")
            except Exception as exc:
                self.last_error = str(exc)
                return False
            self._config = config
            self.last_error = None
            self._record_version(config, "file")
            return True
