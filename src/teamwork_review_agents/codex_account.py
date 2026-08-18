"""通过 Codex App Server 管理独立 Codex Home 的账户状态。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


APP_SERVER_TIMEOUT_SECONDS = 10.0
LOGIN_TIMEOUT_SECONDS = 600.0


class CodexAccountError(RuntimeError):
    """表示无法安全完成 Codex 账户操作。"""


def _resolve_binary(codex_binary: str) -> str:
    """解析 Codex 命令，避免 App Server 启动时依赖不确定的工作目录。"""

    resolved = shutil.which(codex_binary)
    if resolved:
        return resolved
    candidate = Path(codex_binary).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    raise CodexAccountError(f"找不到 Codex CLI：{codex_binary}")


def _codex_environment(home: Path) -> dict[str, str]:
    """构造只覆盖 Codex Home 的 App Server 环境。"""

    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(home)
    return environment


class CodexAppServer:
    """封装一个基于标准输入输出的 Codex App Server 连接。"""

    def __init__(
        self,
        codex_binary: str,
        home: Path,
        working_directory: Path | None = None,
    ) -> None:
        self.codex_binary = codex_binary
        self.home = home.expanduser().resolve()
        self.working_directory = (
            working_directory.expanduser().resolve()
            if working_directory is not None
            else None
        )
        self.process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动 App Server 并完成协议初始化。"""

        command = _resolve_binary(self.codex_binary)
        try:
            self.process = await asyncio.create_subprocess_exec(
                command,
                "app-server",
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_codex_environment(self.home),
                cwd=str(self.working_directory) if self.working_directory else None,
                start_new_session=os.name != "nt",
            )
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "teamwork_review_agents",
                        "title": "Teamwork Review Agents",
                        "version": "0.2.0",
                    }
                },
            )
            await self.notify("initialized", {})
        except Exception:
            await self.close()
            raise

    async def _read_stdout(self) -> None:
        """持续分发响应和通知，不保留 App Server 原始输出。"""

        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while line := await process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if isinstance(request_id, int) and (
                    "result" in message or "error" in message
                ):
                    future = self._pending.get(request_id)
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                if isinstance(message.get("method"), str):
                    await self._notifications.put(message)
        finally:
            error = CodexAccountError("Codex App Server 已退出")
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(error)

    async def _drain_stderr(self) -> None:
        """消费标准错误以避免管道阻塞，但不记录可能包含隐私的信息。"""

        process = self.process
        if process is None or process.stderr is None:
            return
        while await process.stderr.readline():
            pass

    async def _send(self, message: dict[str, Any]) -> None:
        """发送一条换行分隔的 JSON-RPC 消息。"""

        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise CodexAccountError("Codex App Server 未运行")
        process.stdin.write(
            (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        )
        await process.stdin.drain()

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = APP_SERVER_TIMEOUT_SECONDS,
    ) -> Any:
        """发送请求并只返回结构化结果。"""

        self._request_id += 1
        request_id = self._request_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {"method": method, "id": request_id, "params": params or {}}
            )
            response = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise CodexAccountError(f"Codex App Server 请求超时：{method}") from exc
        finally:
            self._pending.pop(request_id, None)
        if response.get("error") is not None:
            error = response.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            raise CodexAccountError(
                f"Codex App Server 请求失败：{method}"
                + (f"（错误码 {code}）" if code is not None else "")
            )
        return response.get("result")

    async def notify(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """发送无需响应的协议通知。"""

        await self._send({"method": method, "params": params or {}})

    async def next_notification(self, timeout: float) -> dict[str, Any]:
        """等待下一条 App Server 通知。"""

        try:
            return await asyncio.wait_for(self._notifications.get(), timeout=timeout)
        except TimeoutError as exc:
            raise CodexAccountError("等待 Codex 登录结果超时") from exc

    async def close(self) -> None:
        """关闭连接并确保 App Server 进程组退出。"""

        process = self.process
        if process is not None and process.stdin is not None:
            process.stdin.close()
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2)
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task
        self.process = None

    async def __aenter__(self) -> CodexAppServer:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


async def read_codex_effective_config(
    codex_binary: str,
    home: Path,
) -> dict[str, Any]:
    """通过官方 App Server 接口读取完成分层后的 Codex 配置。"""

    inspection_directory = home if home.is_dir() else Path.home()
    async with CodexAppServer(
        codex_binary,
        home,
        working_directory=inspection_directory,
    ) as server:
        result = await server.request("config/read", {"includeLayers": False})
    config = result.get("config") if isinstance(result, dict) else None
    if not isinstance(config, dict):
        raise CodexAccountError("Codex App Server 未返回有效配置")
    return config


def _safe_window(value: Any) -> dict[str, Any] | None:
    """只保留额度窗口中用于展示的非敏感字段。"""

    if not isinstance(value, dict):
        return None
    return {
        key: value.get(key)
        for key in ("usedPercent", "windowDurationMins", "resetsAt")
        if value.get(key) is not None
    }


def _safe_limit(value: Any) -> dict[str, Any] | None:
    """把额度桶裁剪为稳定的展示字段。"""

    if not isinstance(value, dict):
        return None
    result = {
        key: value.get(key)
        for key in ("limitId", "limitName", "planType", "rateLimitReachedType")
        if value.get(key) is not None
    }
    primary = _safe_window(value.get("primary"))
    secondary = _safe_window(value.get("secondary"))
    if primary is not None:
        result["primary"] = primary
    if secondary is not None:
        result["secondary"] = secondary
    return result


def _safe_rate_limits(value: Any) -> dict[str, Any] | None:
    """裁剪额度响应，禁止把未知服务字段直接暴露给浏览器。"""

    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    primary = _safe_limit(value.get("rateLimits"))
    if primary is not None:
        result["rateLimits"] = primary
    raw_by_id = value.get("rateLimitsByLimitId")
    if isinstance(raw_by_id, dict):
        by_id = []
        for key, item in sorted(raw_by_id.items()):
            safe = _safe_limit(item)
            if safe is not None:
                safe.setdefault("limitId", str(key))
                by_id.append(safe)
        result["rateLimitsByLimitId"] = by_id
    reset_credits = value.get("rateLimitResetCredits")
    if isinstance(reset_credits, dict) and isinstance(
        reset_credits.get("availableCount"), int
    ):
        result["rateLimitResetCredits"] = {
            "availableCount": reset_credits["availableCount"]
        }
    return result or None


def _safe_usage(value: Any) -> dict[str, Any] | None:
    """裁剪账户用量摘要，不返回服务端附带的未知字段。"""

    if not isinstance(value, dict) or not isinstance(value.get("summary"), dict):
        return None
    summary = {
        key: value["summary"].get(key)
        for key in (
            "lifetimeTokens",
            "peakDailyTokens",
            "longestRunningTurnSec",
            "currentStreakDays",
            "longestStreakDays",
        )
        if value["summary"].get(key) is not None
    }
    return {"summary": summary} if summary else None


async def inspect_codex_account(
    codex_binary: str,
    configured_home: Path | None,
) -> dict[str, Any]:
    """读取已保存独立 Codex Home 的脱敏账户与额度信息。"""

    if configured_home is None:
        return {
            "managed": False,
            "status": "inherited",
            "account": None,
            "rate_limits": None,
            "usage": None,
        }
    home = configured_home.expanduser().resolve()
    if not home.exists():
        return {
            "managed": True,
            "status": "signed_out",
            "codex_home": str(home),
            "account": None,
            "rate_limits": None,
            "usage": None,
        }
    async with CodexAppServer(codex_binary, home) as server:
        account_result = await server.request(
            "account/read", {"refreshToken": False}
        )
        account = (
            account_result.get("account")
            if isinstance(account_result, dict)
            else None
        )
        safe_account = None
        if isinstance(account, dict):
            safe_account = {
                key: account.get(key)
                for key in ("type", "email", "planType", "credentialSource")
                if account.get(key) is not None
            }
        response: dict[str, Any] = {
            "managed": True,
            "status": "signed_in" if safe_account else "signed_out",
            "codex_home": str(home),
            "requires_openai_auth": account_result.get("requiresOpenaiAuth")
            if isinstance(account_result, dict)
            else None,
            "account": safe_account,
            "rate_limits": None,
            "usage": None,
        }
        if not safe_account or safe_account.get("type") != "chatgpt":
            return response
        try:
            response["rate_limits"] = _safe_rate_limits(
                await server.request("account/rateLimits/read")
            )
        except CodexAccountError as exc:
            response["rate_limits_error"] = str(exc)
        try:
            response["usage"] = _safe_usage(
                await server.request("account/usage/read")
            )
        except CodexAccountError as exc:
            response["usage_error"] = str(exc)
        return response


@dataclass
class LoginSession:
    """保存一次不会落盘的浏览器登录会话。"""

    session_id: str
    login_id: str
    codex_home: str
    auth_url: str
    status: str = "pending"
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    server: CodexAppServer | None = field(default=None, repr=False)
    watcher: asyncio.Task[None] | None = field(default=None, repr=False)

    def snapshot(self) -> dict[str, Any]:
        """返回不包含认证凭据和内部对象的会话状态。"""

        return {
            "session_id": self.session_id,
            "codex_home": self.codex_home,
            "auth_url": self.auth_url,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class CodexLoginManager:
    """管理各独立 Codex Home 的短期登录进程。"""

    def __init__(self) -> None:
        self._sessions: dict[str, LoginSession] = {}
        self._lock = asyncio.Lock()

    async def start(self, codex_binary: str, home: Path) -> dict[str, Any]:
        """启动浏览器登录；相同 Home 已登录中时复用现有会话。"""

        resolved_home = home.expanduser().resolve()
        home_key = str(resolved_home)
        async with self._lock:
            for session in self._sessions.values():
                if session.codex_home == home_key and session.status == "pending":
                    return session.snapshot()
            existed = resolved_home.exists()
            resolved_home.mkdir(parents=True, mode=0o700, exist_ok=True)
            if not existed:
                resolved_home.chmod(0o700)
            server = CodexAppServer(codex_binary, resolved_home)
            try:
                await server.start()
                result = await server.request(
                    "account/login/start",
                    {
                        "type": "chatgpt",
                        "useHostedLoginSuccessPage": True,
                        "appBrand": "codex",
                    },
                )
            except Exception:
                await server.close()
                raise
            login_id = result.get("loginId") if isinstance(result, dict) else None
            auth_url = result.get("authUrl") if isinstance(result, dict) else None
            if not isinstance(login_id, str) or not isinstance(auth_url, str):
                await server.close()
                raise CodexAccountError("Codex App Server 未返回登录地址")
            session = LoginSession(
                session_id=str(uuid.uuid4()),
                login_id=login_id,
                codex_home=home_key,
                auth_url=auth_url,
                server=server,
            )
            self._sessions[session.session_id] = session
            session.watcher = asyncio.create_task(self._watch(session))
            return session.snapshot()

    async def _watch(self, session: LoginSession) -> None:
        """等待指定登录完成并在任何终态回收进程。"""

        server = session.server
        if server is None:
            return
        deadline = asyncio.get_running_loop().time() + LOGIN_TIMEOUT_SECONDS
        try:
            while session.status == "pending":
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise CodexAccountError("Codex 登录等待超时")
                message = await server.next_notification(remaining)
                if message.get("method") != "account/login/completed":
                    continue
                params = message.get("params")
                if not isinstance(params, dict) or params.get("loginId") != session.login_id:
                    continue
                if params.get("success") is True:
                    session.status = "completed"
                else:
                    session.status = "failed"
                    session.error = "Codex 登录未完成，请重新发起登录"
                break
        except asyncio.CancelledError:
            raise
        except CodexAccountError as exc:
            if session.status == "pending":
                session.status = "failed"
                session.error = str(exc)
        finally:
            session.finished_at = time.time()
            await server.close()
            session.server = None

    def get(self, session_id: str) -> dict[str, Any] | None:
        """读取一次登录会话的脱敏快照。"""

        session = self._sessions.get(session_id)
        return session.snapshot() if session is not None else None

    async def cancel(self, session_id: str) -> dict[str, Any] | None:
        """取消等待中的登录并回收 App Server。"""

        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.status != "pending":
            return session.snapshot()
        server = session.server
        session.status = "cancelled"
        session.finished_at = time.time()
        if server is not None:
            with suppress(CodexAccountError):
                await server.request(
                    "account/login/cancel", {"loginId": session.login_id}
                )
        if session.watcher is not None and not session.watcher.done():
            session.watcher.cancel()
            with suppress(asyncio.CancelledError):
                await session.watcher
        if server is not None:
            await server.close()
        session.server = None
        return session.snapshot()

    async def close(self) -> None:
        """服务退出时取消全部未完成登录。"""

        for session_id in list(self._sessions):
            await self.cancel(session_id)
