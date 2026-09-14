"""服务级 Windows curl 自动准备；管理页面启动不等待网络或沙盒探测。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import portalocker

from .config import AppConfig
from .curl_distribution import (
    CurlPreparationError, curl_runtime_root, distribution_for_machine, download_archive,
    ensure_cache_directories, installed_candidate, publish_archive, read_verified_archive, require_plain_path,
)
from .process_control import process_group_options, terminate_process
from .sandbox_curl import CurlCandidate, curl_candidates, openssl_curl_version
from .subprocess_utils import WINDOWS_REQUIRED_ENVIRONMENT_NAMES, selected_environment

_PROBE_TIMEOUT_SECONDS = 15
_PREPARATION_TIMEOUT_SECONDS = 90


def windows_curl_runtime_enabled() -> bool:
    """仅 Windows 需要自动准备，平台判断独立以便跨平台回归。"""

    return os.name == "nt" or sys.platform == "win32"


async def inspect_installed_curl(candidate: CurlCandidate, config: AppConfig) -> dict[str, Any]:
    """直接在禁网原生沙盒中探测程序，避免依赖服务 Python 或 Agent 临时目录。"""

    from .managed_sandbox import inspect_managed_sandbox
    from .sandbox_environment import sandbox_host_environment

    inspection = await asyncio.to_thread(inspect_managed_sandbox, config.runtime.codex_binary, config.runtime.codex_home)
    if not inspection.available:
        raise CurlPreparationError("sandbox_unavailable", "Windows 外层沙盒尚不可用，请先检查 Codex 安装与沙盒初始化；这不是缺少 OpenSSL。")
    if not candidate.executable.is_file():
        return {"code": "executable_missing", "message": "未找到指定 curl 程序。"}
    if candidate.executable.name.lower() != "curl.exe" or candidate.executable.parent.parent == candidate.executable.parent:
        return {"code": "invalid_executable", "message": "curl 路径必须指向安装目录中的 curl.exe。"}
    environment = selected_environment(WINDOWS_REQUIRED_ENVIRONMENT_NAMES | {"PATH", "HOME", "CODEX_HOME"})
    environment = sandbox_host_environment(environment, codex_home=config.runtime.codex_home)
    environment["CURL_SSL_BACKEND"] = "openssl"
    entries = ",".join(f'{json.dumps(str(path), ensure_ascii=False)}="read"' for path in candidate.readable_directories)
    profile = f'permissions.teamwork_curl_probe={{extends=":read-only",filesystem={{{entries}}},network={{enabled=false}}}}'
    with tempfile.TemporaryDirectory(prefix="teamwork-curl-probe-") as workspace:
        command = [inspection.resolved_path, "sandbox", "--permission-profile", "teamwork_curl_probe", "--cd", workspace,
                   "--config", profile, "--", str(candidate.executable), "-q", "--version"]
        readers: list[asyncio.Task] = []
        try:
            process = await asyncio.create_subprocess_exec(
                *command, env=environment, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **process_group_options(),
            )
        except OSError:
            return {"code": "sandbox_process_denied", "message": "沙盒无法启动 curl，请检查程序执行权限。"}
        try:
            # 帮助输出有界，防止不受信任的候选持续输出耗尽服务内存。
            async def read_limited(stream):
                """读满上限即报错，最终由统一进程树收尾终止候选。"""

                data = bytearray()
                while chunk := await stream.read(8192):
                    data.extend(chunk)
                    if len(data) > 65536:
                        raise CurlPreparationError("probe_output_limit", "curl 版本探针输出异常，已终止。")
                return bytes(data)

            readers = [asyncio.create_task(read_limited(process.stdout)), asyncio.create_task(read_limited(process.stderr))]
            async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
                output, errors, _ = await asyncio.gather(*readers, process.wait())
            if process.returncode:
                # 不回显外层命令/环境，只返回固定类别，避免诊断泄露凭据。
                denied = b"CreateProcessAsUserW failed: 5" in errors or b"Windows error 5" in errors
                return {"code": "sandbox_process_denied" if denied else "version_probe_failed",
                        "message": "沙盒无法执行 curl，请检查安装目录的执行权限。" if denied else "curl 版本探针失败，请查看程序安装与沙盒状态。"}
            backend = openssl_curl_version(output.decode("utf-8", errors="replace"))
            return {"code": "ready", "backend": backend} if backend else {"code": "backend_incompatible", "message": "当前 curl 未启用兼容的 TLS 后端或不支持 HTTPS。"}
        except TimeoutError:
            return {"code": "version_probe_timeout", "message": "curl 程序执行验证超时，不重复下载安装。"}
        finally:
            # 异常输出或取消可能只结束一个读取器，必须同时回收另一个管道任务。
            for reader in readers:
                reader.cancel()
            if process.returncode is None:
                terminate_process(process.pid, force=True, tree=True)
                await process.wait()
            await asyncio.gather(*readers, return_exceptions=True)


class CurlRuntimeManager:
    """按部署配置合并准备请求，缓存状态与首次准备屏障均只属于本服务。"""

    def __init__(self, config: Callable[[], AppConfig]) -> None:
        self.config = config
        self._task: asyncio.Task[None] | None = None
        self._key: tuple | None = None
        self._closed = False
        self._status: dict[str, Any] = {"status": "idle", "message": "等待服务初始化。"}

    def snapshot(self) -> dict[str, Any]:
        """GET 不产生文件、网络请求或安装动作。"""

        state = dict(self._status)
        root = curl_runtime_root(self.config())
        state["cache_directory"] = str(root)
        try:
            distribution = distribution_for_machine()
            state["offline_archive"] = str(root / "downloads" / distribution.filename)
            state["offline_download_url"] = distribution.url
        except CurlPreparationError:
            state["offline_archive"] = None
        return state

    def start(self, *, retry: bool = False) -> dict[str, Any]:
        """启动异步准备，连续点击合并；相关配置变化先取消旧准备再使用新配置。"""

        if self._closed:
            return self.snapshot()
        config = self.config()
        managed = config.runtime.managed_sandbox
        key = (str(curl_runtime_root(config)), managed.enabled, managed.curl_auto_prepare,
               str(managed.curl_binary), config.runtime.codex_binary, str(config.runtime.codex_home))
        if key == self._key and (not retry or (self._task is not None and not self._task.done())):
            return self.snapshot()
        previous = self._task
        if previous is not None and not previous.done():
            previous.cancel()
        self._key = key
        self._status = {"status": "preparing", "message": "正在检查兼容 curl，无需手动配置 OpenSSL。"}

        async def run():
            """旧任务清理完成后再开始新准备，防止配置热加载时交叉发布。"""

            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            self._status = {"status": "preparing", "message": "正在检查兼容 curl，无需手动配置 OpenSSL。"}
            await self._prepare(config)

        self._task = asyncio.create_task(run(), name="teamwork-curl-runtime")
        return self.snapshot()

    async def wait(self) -> None:
        """首轮调度等待有界初始化，失败不封锁后续任务。"""

        while self._task is not None and not self._task.done():
            task = self._task
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)

    async def close(self) -> None:
        """停止下载/探针并完成临时文件收尾，不能只取消界面状态。"""

        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _ready(self, candidate: CurlCandidate, diagnostic: dict[str, Any]) -> None:
        """程序就绪不代表所有仓库网络或证书策略均已通过。"""

        self._status = {"status": "ready", "message": "兼容 curl 已就绪；实际 HTTPS 按每个 Agent 的权限检查。",
                        "source": candidate.source, "curl_binary": str(candidate.executable), "ssl_backend": diagnostic["backend"]}

    async def _prepare(self, config: AppConfig) -> None:
        """仅缺少程序/后端不兼容时准备分发；TLS 请求错误不能触发安装。"""

        managed = config.runtime.managed_sandbox
        lock = None
        try:
            if not windows_curl_runtime_enabled() or not managed.enabled:
                self._status = {"status": "not_applicable", "message": "当前环境无需 Windows curl 自动准备。"}
                return
            if not managed.curl_auto_prepare and managed.curl_binary is None:
                self._status = {"status": "disabled", "message": "已关闭 curl 自动准备，现有程序仍可由 Agent 检测使用。"}
                return
            async with asyncio.timeout(_PREPARATION_TIMEOUT_SECONDS):
                root = curl_runtime_root(config)
                candidates = curl_candidates(managed.curl_binary, os.environ)
                if managed.curl_binary is None:
                    installed = installed_candidate(root)
                    if installed:
                        candidates.insert(0, CurlCandidate(installed[0], "managed_cache", installed[1]))
                failures: list[dict[str, str]] = []
                for candidate in candidates:
                    diagnostic = await inspect_installed_curl(candidate, config)
                    if diagnostic["code"] == "ready":
                        self._ready(candidate, diagnostic)
                        return
                    failures.append({"path": str(candidate.executable), **diagnostic})
                if managed.curl_binary is not None or any(item["code"] not in {"executable_missing", "backend_incompatible"} for item in failures):
                    self._status = {"status": "unavailable", "message": "已有 curl 未通过执行验证；未重复下载，请检查下方具体原因。", "candidates": failures}
                    return
                distribution = distribution_for_machine()
                ensure_cache_directories(root)
                lock_path = root / "prepare.lock"
                if lock_path.exists() or lock_path.is_symlink():
                    require_plain_path(lock_path, directory=False)
                lock = portalocker.Lock(str(lock_path), mode="a", timeout=0, flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
                while True:
                    try:
                        lock.acquire()
                        break
                    except portalocker.exceptions.LockException:
                        await asyncio.sleep(0.2)
                # 另一进程可能已经发布，锁内再检查以避免重复下载或替换。
                installed = installed_candidate(root, distribution)
                if installed is None:
                    archive_path = root / "downloads" / distribution.filename
                    if archive_path.exists() or archive_path.is_symlink():
                        try:
                            read_verified_archive(archive_path, distribution)
                        except CurlPreparationError as exc:
                            if exc.code == "archive_integrity_failed":
                                archive_path.rename(archive_path.with_name(f".{archive_path.name}.invalid-{uuid.uuid4().hex}"))
                                raise CurlPreparationError(exc.code, "已有 curl 安装包校验失败，原文件已隔离保存；请重试下载或提供对应版本的官方离线包。") from exc
                            raise
                        source = "offline_or_cached_archive"
                    else:
                        self._status = {"status": "preparing", "message": "正在下载并校验项目专用 curl，管理页面可继续使用。"}
                        archive_path = await download_archive(root, distribution)
                        source = "official_download"
                    installed = publish_archive(root, distribution, archive_path)
                else:
                    source = "managed_cache"
                candidate = CurlCandidate(installed[0], source, installed[1])
                diagnostic = await inspect_installed_curl(candidate, config)
                if diagnostic["code"] != "ready":
                    self._status = {"status": "unavailable", "message": "安装包已准备，但沙盒程序验证未通过；无需重新安装 OpenSSL。",
                                    "curl_binary": str(candidate.executable), "candidates": [{"path": str(candidate.executable), **diagnostic}]}
                else:
                    self._ready(candidate, diagnostic)
        except asyncio.CancelledError:
            self._status = {"status": "cancelled", "message": "curl 准备已取消，临时下载已清理。"}
            raise
        except TimeoutError:
            self._status = {"status": "unavailable", "error_code": "preparation_timeout", "message": "curl 准备超时，管理服务继续运行，可稍后重试或使用离线包。"}
        except CurlPreparationError as exc:
            self._status = {"status": "unavailable", "error_code": exc.code, "message": str(exc)}
        except Exception:
            # 文件系统或第三方异常可能包含敏感环境信息，只返回固定诊断。
            self._status = {"status": "unavailable", "error_code": "preparation_failed", "message": "curl 自动准备失败，请检查数据目录权限、磁盘空间和安装包；管理服务继续运行。"}
        finally:
            if lock is not None:
                lock.release()
            self._status["updated_at"] = time.time()
