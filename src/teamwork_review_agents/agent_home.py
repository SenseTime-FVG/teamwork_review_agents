"""为后台 Agent 提供每次运行独立的临时用户目录。"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .process_control import pid_exists


_INSTANCE_ID = uuid.uuid4().hex[:12]
_DIRECTORY_PATTERN = re.compile(
    r"^(?P<pid>\d+)-(?P<instance>[0-9a-f]{12})-[A-Za-z0-9_-]+-"
)
_STALE_CLEANUP_LOCK = threading.Lock()
_STALE_CLEANUP_COMPLETED = False
_CODEX_RUNTIME_INPUTS = {
    "auth.json": "CODEX_AUTH",
    ".credentials.json": "CODEX_CREDENTIALS",
    "config.toml": "CODEX_CONFIG",
    "requirements.toml": "CODEX_REQUIREMENTS",
}


def _owner_key() -> str:
    """生成只用于隔离系统临时目录的当前用户标识。"""

    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        return str(getuid())
    value = os.environ.get("USERNAME") or os.environ.get("USER") or "default"
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    return normalized or "default"


def agent_home_root() -> Path:
    """返回当前用户专用的 Agent 临时 HOME 根目录。"""

    return Path(tempfile.gettempdir()) / f"teamwork-agent-homes-{_owner_key()}"


def agent_codex_home_root(
    host_environment: Mapping[str, str] | None = None,
    *,
    platform_name: str | None = None,
    os_name: str | None = None,
) -> Path:
    """返回不会触发 Codex 临时目录保护的每轮运行根目录。"""

    host = host_environment or os.environ
    active_platform = platform_name or sys.platform
    active_os = os_name or os.name
    host_home_value = host.get("HOME") or host.get("USERPROFILE")
    host_home = (
        Path(host_home_value).expanduser() if host_home_value else Path.home()
    )
    if active_os == "nt" or active_platform == "win32":
        cache_root = _environment_path(host, "LOCALAPPDATA")
        if cache_root is None:
            cache_root = host_home / "AppData/Local"
    elif active_platform == "darwin":
        cache_root = host_home / "Library/Caches"
    else:
        cache_root = _environment_path(host, "XDG_CACHE_HOME")
        if cache_root is None:
            cache_root = host_home / ".cache"
    return cache_root / "teamwork-review-agents/agent-codex-homes"


def _ensure_root(path: Path) -> Path:
    """安全创建临时运行根目录，并拒绝固定根路径被替换为符号链接。"""

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"Agent 临时运行根目录不安全：{path}")
    try:
        path.chmod(0o700)
    except OSError:
        # Windows 等平台可能无法完整表达 POSIX 权限，目录仍由当前用户创建。
        pass
    return path


def cleanup_stale_agent_homes(root: Path | None = None) -> list[Path]:
    """删除所属服务进程已经不存在的遗留临时 HOME。"""

    target_root = root or agent_home_root()
    if not target_root.exists() or target_root.is_symlink():
        return []
    removed: list[Path] = []
    for path in target_root.iterdir():
        matched = _DIRECTORY_PATTERN.match(path.name)
        if matched is None:
            continue
        owner_pid = int(matched.group("pid"))
        owner_instance = matched.group("instance")
        if owner_instance == _INSTANCE_ID or pid_exists(owner_pid):
            continue
        try:
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


def cleanup_stale_agent_homes_once() -> None:
    """每个服务进程只执行一次遗留目录回收。"""

    global _STALE_CLEANUP_COMPLETED
    with _STALE_CLEANUP_LOCK:
        if _STALE_CLEANUP_COMPLETED:
            return
        for root in (agent_home_root(), agent_codex_home_root()):
            try:
                cleanup_stale_agent_homes(root)
            except OSError:
                # 遗留目录回收是尽力而为，不能阻断新的 Agent 运行。
                pass
        _STALE_CLEANUP_COMPLETED = True


def _existing_directory(*candidates: Path | None) -> Path | None:
    """返回第一个已经存在的目录，不为宿主机配置主动创建路径。"""

    for candidate in candidates:
        if candidate is not None and candidate.is_dir():
            return candidate
    return None


def _environment_path(environment: Mapping[str, str], name: str) -> Path | None:
    """读取环境中的非空路径，保留调用者显式选择。"""

    value = environment.get(name)
    return Path(value).expanduser() if value else None


def _is_windows() -> bool:
    """返回当前进程是否运行在原生 Windows。"""

    return os.name == "nt"


def _bridge_macos_keychains(temporary_home: Path, host_home: Path) -> bool:
    """让临时 HOME 继续使用当前 macOS 用户的系统钥匙串搜索入口。"""

    if sys.platform != "darwin":
        return False
    host_keychains = host_home / "Library/Keychains"
    if not host_keychains.is_dir():
        return False
    bridge_parent = temporary_home / "Library"
    bridge_parent.mkdir(parents=True, exist_ok=True)
    bridge = bridge_parent / "Keychains"
    host_target = host_keychains.resolve()

    def reuse_existing_bridge() -> bool:
        """只复用目标完全一致的链接，拒绝覆盖异常占用路径。"""

        try:
            matches = bridge.is_symlink() and bridge.resolve(strict=True) == host_target
        except OSError:
            matches = False
        if matches:
            return True
        raise RuntimeError(
            f"Agent 临时 HOME 的 macOS Keychain 桥接路径已被占用：{bridge}"
        )

    if bridge.is_symlink() or bridge.exists():
        return reuse_existing_bridge()
    try:
        bridge.symlink_to(host_target, target_is_directory=True)
    except FileExistsError:
        # 防止两个环境准备步骤恰好同时创建同一桥接。
        return reuse_existing_bridge()
    return True


@dataclass
class TemporaryAgentHome:
    """一次 Agent 运行所拥有的临时 HOME 与认证配置桥接。"""

    path: Path
    _manager: tempfile.TemporaryDirectory[str] = field(repr=False)
    bridges: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        run_id: str,
        *,
        root: Path | None = None,
    ) -> "TemporaryAgentHome":
        """创建权限收紧的 HOME 骨架，目录名保留运行归属信息。"""

        target_root = _ensure_root(root or agent_home_root())
        safe_run_id = re.sub(r"[^A-Za-z0-9_-]+", "-", run_id).strip("-")[:24]
        safe_run_id = safe_run_id or "run"
        manager = tempfile.TemporaryDirectory(
            prefix=f"{os.getpid()}-{_INSTANCE_ID}-{safe_run_id}-",
            dir=target_root,
        )
        path = Path(manager.name)
        for relative in (
            ".cache",
            ".config",
            ".local/share",
            ".local/state",
            "tmp",
        ):
            (path / relative).mkdir(parents=True, exist_ok=True)
        return cls(path=path, _manager=manager)

    def apply_environment(
        self,
        environment: dict[str, str],
        *,
        codex_home: Path,
        host_environment: Mapping[str, str] | None = None,
    ) -> tuple[str, ...]:
        """切换通用用户目录，并只桥接已存在的宿主机配置入口。"""

        host = host_environment or os.environ
        host_home_value = host.get("HOME") or host.get("USERPROFILE")
        host_home = (
            Path(host_home_value).expanduser() if host_home_value else Path.home()
        )
        host_xdg = _environment_path(host, "XDG_CONFIG_HOME")

        # 这些目录全部属于当前运行，仓库程序无需声明专用 HOME 变量。
        environment["HOME"] = str(self.path)
        environment["XDG_CACHE_HOME"] = str(self.path / ".cache")
        environment["XDG_CONFIG_HOME"] = str(self.path / ".config")
        environment["XDG_DATA_HOME"] = str(self.path / ".local/share")
        environment["XDG_STATE_HOME"] = str(self.path / ".local/state")
        if _is_windows():
            appdata = self.path / "AppData/Roaming"
            local_appdata = self.path / "AppData/Local"
            appdata.mkdir(parents=True, exist_ok=True)
            local_appdata.mkdir(parents=True, exist_ok=True)
            environment["USERPROFILE"] = str(self.path)
            environment["APPDATA"] = str(appdata)
            environment["LOCALAPPDATA"] = str(local_appdata)

        bridges: list[str] = []
        environment["CODEX_HOME"] = str(codex_home)
        bridges.append("CODEX_HOME")

        if _bridge_macos_keychains(self.path, host_home):
            bridges.append("MACOS_KEYCHAINS")

        gh_config = _environment_path(host, "GH_CONFIG_DIR") or _existing_directory(
            host_xdg / "gh" if host_xdg is not None else None,
            host_home / ".config/gh",
        )
        if gh_config is not None:
            environment["GH_CONFIG_DIR"] = str(gh_config)
            bridges.append("GH_CONFIG_DIR")

        glab_config = _environment_path(host, "GLAB_CONFIG_DIR") or _existing_directory(
            host_xdg / "glab-cli" if host_xdg is not None else None,
            host_home / ".config/glab-cli",
            host_home / ".config/glab",
        )
        if glab_config is not None:
            environment["GLAB_CONFIG_DIR"] = str(glab_config)
            bridges.append("GLAB_CONFIG_DIR")

        git_config = _environment_path(host, "GIT_CONFIG_GLOBAL")
        if git_config is None:
            candidate = host_home / ".gitconfig"
            git_config = candidate if candidate.is_file() else None
        if git_config is not None:
            environment["GIT_CONFIG_GLOBAL"] = str(git_config)
            bridges.append("GIT_CONFIG_GLOBAL")

        for name in ("SSH_AUTH_SOCK", "SSH_AGENT_PID"):
            value = host.get(name)
            if value:
                environment[name] = value
                bridges.append(name)

        self.bridges = tuple(bridges)
        return self.bridges

    def cleanup(self) -> str | None:
        """清理当前运行目录，失败时返回可记录但不覆盖任务结果的错误。"""

        try:
            self._manager.cleanup()
        except OSError as exc:
            return str(exc)
        return None


@dataclass
class TemporaryCodexHome:
    """外层沙盒内供 Codex 宿主进程写入的每轮运行目录。"""

    path: Path
    _manager: tempfile.TemporaryDirectory[str] = field(repr=False)
    bridges: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        run_id: str,
        *,
        source_home: Path,
        root: Path | None = None,
        host_environment: Mapping[str, str] | None = None,
    ) -> "TemporaryCodexHome":
        """创建独立运行目录，并复制必要的登录与配置快照。"""

        target_root = _ensure_root(
            root or agent_codex_home_root(host_environment)
        )
        safe_run_id = re.sub(r"[^A-Za-z0-9_-]+", "-", run_id).strip("-")[:24]
        safe_run_id = safe_run_id or "run"
        manager = tempfile.TemporaryDirectory(
            prefix=f"{os.getpid()}-{_INSTANCE_ID}-{safe_run_id}-",
            dir=target_root,
        )
        path = Path(manager.name)
        bridges: list[str] = []
        try:
            for name, bridge_name in _CODEX_RUNTIME_INPUTS.items():
                source = source_home / name
                if not source.is_file():
                    continue
                target = path / name
                shutil.copy2(source, target)
                try:
                    target.chmod(0o600)
                except OSError:
                    # Windows 等平台可能无法完整表达 POSIX 文件权限。
                    pass
                bridges.append(bridge_name)
        except Exception:
            manager.cleanup()
            raise
        return cls(
            path=path,
            _manager=manager,
            bridges=tuple(bridges),
        )

    def apply_environment(self, environment: dict[str, str]) -> tuple[str, ...]:
        """幂等地把 Codex 状态与临时产物定向到本轮目录。"""

        environment["CODEX_HOME"] = str(self.path)
        return self.bridges

    def cleanup(self) -> str | None:
        """清理本轮 Codex 运行目录，不回写任何宿主文件。"""

        try:
            self._manager.cleanup()
        except OSError as exc:
            return str(exc)
        return None
