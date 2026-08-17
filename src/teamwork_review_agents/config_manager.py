"""YAML 配置热加载、原子保存、Secret 占位合并和版本记录。"""

from __future__ import annotations

import copy
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml

from .config import AppConfig, load_config, parse_config_data
from .environment import MASK
from .state import StateStore


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
        return raw

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
            raw = self._read_raw()
        return self._mask_secrets(raw) if mask_secrets else raw

    def _record_version(self, config: AppConfig, source: str) -> None:
        """只保存脱敏后的配置历史。"""

        masked = self._mask_secrets(self._read_raw())
        content = yaml.safe_dump(masked, allow_unicode=True, sort_keys=False)
        self.store.save_config_version(config.revision, content, source)

    def validate(self, document: dict[str, Any]) -> AppConfig:
        """合并 Secret 占位并执行完整配置校验。"""

        current = self._read_raw()
        merged = self._merge_masked(document, current)
        return parse_config_data(merged, self.path)

    def save(self, document: dict[str, Any], *, source: str = "ui") -> AppConfig:
        """校验后在同目录原子替换 YAML，并更新有效配置。"""

        with self._lock:
            current_raw = self._read_raw()
            merged = self._merge_masked(document, current_raw)
            config = parse_config_data(merged, self.path)
            if config.database.path != self._config.database.path:
                raise ValueError("后台运行期间不允许通过 UI 修改 database.path")
            content = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False)
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
