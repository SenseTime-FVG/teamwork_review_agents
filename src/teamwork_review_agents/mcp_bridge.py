"""在托管沙盒内外传递受控 MCP 调用的临时文件通道。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .process_control import process_group_options, terminate_process


CHANNEL_VERSION = 1
CHANNEL_DIRECTORY_ENV = "TEAMWORK_MCP_CHANNEL_DIR"
CHANNEL_TOKEN_ENV = "TEAMWORK_MCP_CHANNEL_TOKEN"
RESPONSE_TIMEOUT_ENV = "TEAMWORK_MCP_RESPONSE_TIMEOUT_SECONDS"
_BROKER_POLL_SECONDS = 0.05
_BROKER_STARTUP_TIMEOUT_SECONDS = 10.0
_BROKER_STOP_GRACE_SECONDS = 10.0
_BROKER_KILL_GRACE_SECONDS = 1.0


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """在同一目录内原子发布 JSON，避免另一端读到半份请求。"""

    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json_object(path: Path) -> dict[str, Any]:
    """读取并校验通道中的 JSON 对象。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("通道消息必须是 JSON 对象")
    return value


def _valid_request_id(value: str) -> bool:
    """只接受规范 UUID，防止请求 ID 逃逸出通道目录。"""

    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


@dataclass(frozen=True)
class McpBridgeChannel:
    """传给沙盒内 MCP 代理的最小通道描述。"""

    directory: Path
    token: str
    response_timeout_seconds: float

    @property
    def requests_directory(self) -> Path:
        """返回待处理请求目录。"""

        return self.directory / "requests"

    @property
    def processing_directory(self) -> Path:
        """返回 Broker 已认领请求目录。"""

        return self.directory / "processing"

    @property
    def responses_directory(self) -> Path:
        """返回响应目录。"""

        return self.directory / "responses"

    @property
    def cancellations_directory(self) -> Path:
        """返回工具调用取消通知目录。"""

        return self.directory / "cancellations"

    @property
    def ready_path(self) -> Path:
        """返回 Broker 就绪标记。"""

        return self.directory / "ready.json"

    @property
    def error_path(self) -> Path:
        """返回 Broker 启动错误标记。"""

        return self.directory / "error.json"

    @property
    def stop_path(self) -> Path:
        """返回 Broker 停止请求标记。"""

        return self.directory / "stop.json"

    def environment_overrides(self) -> dict[str, str]:
        """生成只包含通道位置、随机令牌和超时的 MCP 环境。"""

        return {
            CHANNEL_DIRECTORY_ENV: str(self.directory),
            CHANNEL_TOKEN_ENV: self.token,
            RESPONSE_TIMEOUT_ENV: str(self.response_timeout_seconds),
        }

    @classmethod
    def create(
        cls,
        run_id: str,
        *,
        response_timeout_seconds: float,
    ) -> "McpBridgeChannel":
        """为一次根或子 Agent 运行创建独立的临时通道。"""

        safe_run_id = "".join(
            character if character.isalnum() else "-" for character in run_id
        )[:36]
        directory = Path(
            tempfile.mkdtemp(prefix=f"teamwork-mcp-{safe_run_id}-")
        ).resolve()
        with suppress(OSError):
            directory.chmod(0o700)
        for child_name in ("requests", "processing", "responses", "cancellations"):
            child = directory / child_name
            child.mkdir(mode=0o700)
        return cls(
            directory=directory,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            response_timeout_seconds=response_timeout_seconds,
        )

    def cleanup(self) -> None:
        """删除整条临时通道及其中的请求和响应。"""

        shutil.rmtree(self.directory, ignore_errors=True)


async def call_bridge(
    channel: McpBridgeChannel,
    *,
    agent_name: str,
    task: str,
    extra_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """从沙盒内提交一次 invoke_agent 请求并等待受控响应。"""

    request_id = str(uuid.uuid4())
    request_path = channel.requests_directory / f"{request_id}.request.json"
    response_path = channel.responses_directory / f"{request_id}.response.json"
    cancellation_path = (
        channel.cancellations_directory / f"{request_id}.cancel.json"
    )
    _atomic_write_json(
        request_path,
        {
            "version": CHANNEL_VERSION,
            "request_id": request_id,
            "token": channel.token,
            "method": "invoke_agent",
            "params": {
                "agent_name": agent_name,
                "task": task,
                "extra_context": extra_context,
            },
        },
    )
    deadline = time.monotonic() + channel.response_timeout_seconds
    try:
        while True:
            if response_path.exists():
                response = _read_json_object(response_path)
                with suppress(OSError):
                    response_path.unlink()
                if response.get("version") != CHANNEL_VERSION:
                    raise RuntimeError("Teamwork MCP Broker 返回了不兼容的协议版本")
                if response.get("request_id") != request_id:
                    raise RuntimeError("Teamwork MCP Broker 返回了不匹配的请求 ID")
                if response.get("token") != channel.token:
                    raise RuntimeError("Teamwork MCP Broker 返回了无效的通道令牌")
                if response.get("ok") is not True:
                    error = response.get("error")
                    message = (
                        str(error.get("message") or "")
                        if isinstance(error, dict)
                        else ""
                    )
                    raise RuntimeError(message or "Teamwork MCP Broker 调用失败")
                result = response.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("Teamwork MCP Broker 返回结果格式无效")
                return result
            if channel.error_path.exists():
                error = _read_json_object(channel.error_path)
                raise RuntimeError(str(error.get("message") or "MCP Broker 不可用"))
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "等待 Teamwork MCP Broker 响应超过 "
                    f"{channel.response_timeout_seconds:g} 秒"
                )
            await asyncio.sleep(_BROKER_POLL_SECONDS)
    except (asyncio.CancelledError, TimeoutError):
        with suppress(OSError):
            _atomic_write_json(
                cancellation_path,
                {
                    "version": CHANNEL_VERSION,
                    "request_id": request_id,
                    "token": channel.token,
                },
            )
        raise


class ManagedMcpBroker:
    """在外层沙盒之外执行原有 invoke_agent 权限校验与编排。"""

    def __init__(self, channel: McpBridgeChannel) -> None:
        self.channel = channel
        self.process: asyncio.subprocess.Process | None = None

    @classmethod
    async def start(
        cls,
        *,
        run_id: str,
        config_path: Path,
        encoded_context: str,
        base_environment: Mapping[str, str],
        response_timeout_seconds: float,
    ) -> "ManagedMcpBroker":
        """创建通道、启动 Broker，并等待其完成最小初始化。"""

        channel = McpBridgeChannel.create(
            run_id,
            response_timeout_seconds=response_timeout_seconds,
        )
        broker = cls(channel)
        environment = dict(base_environment)
        environment.update(channel.environment_overrides())
        environment["TEAMWORK_CONFIG_PATH"] = str(config_path)
        environment["TEAMWORK_INVOCATION_CONTEXT"] = encoded_context
        try:
            broker.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "teamwork_review_agents.mcp_bridge",
                "broker",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                **process_group_options(),
            )
            await broker._wait_until_ready()
            return broker
        except BaseException:
            await broker.close()
            raise

    async def _wait_until_ready(self) -> None:
        """等待 Broker 就绪，避免把启动失败拖到模型工具调用阶段。"""

        assert self.process is not None
        deadline = time.monotonic() + _BROKER_STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.channel.ready_path.exists():
                return
            if self.channel.error_path.exists():
                error = _read_json_object(self.channel.error_path)
                raise RuntimeError(str(error.get("message") or "MCP Broker 启动失败"))
            if self.process.returncode is not None:
                raise RuntimeError(
                    f"MCP Broker 提前退出，退出码：{self.process.returncode}"
                )
            await asyncio.sleep(_BROKER_POLL_SECONDS)
        raise TimeoutError("等待 Teamwork MCP Broker 启动超时")

    async def close(self) -> None:
        """先请求 Broker 收尾，再强制终止残留的子 Agent 进程树。"""

        process = self.process
        try:
            if process is not None and process.returncode is None:
                with suppress(OSError):
                    _atomic_write_json(
                        self.channel.stop_path,
                        {"version": CHANNEL_VERSION, "token": self.channel.token},
                    )
                try:
                    await asyncio.wait_for(
                        process.wait(),
                        timeout=_BROKER_STOP_GRACE_SECONDS,
                    )
                except TimeoutError:
                    with suppress(ProcessLookupError, PermissionError):
                        terminate_process(process.pid, force=False, tree=True)
                    try:
                        await asyncio.wait_for(
                            process.wait(),
                            timeout=_BROKER_KILL_GRACE_SECONDS,
                        )
                    except TimeoutError:
                        with suppress(ProcessLookupError, PermissionError):
                            terminate_process(process.pid, force=True, tree=True)
                        with suppress(TimeoutError):
                            await asyncio.wait_for(
                                process.wait(),
                                timeout=_BROKER_KILL_GRACE_SECONDS,
                            )
        finally:
            self.process = None
            self.channel.cleanup()


def channel_from_environment() -> McpBridgeChannel:
    """从沙盒内 MCP 代理的环境恢复通道描述。"""

    directory = os.environ.get(CHANNEL_DIRECTORY_ENV)
    token = os.environ.get(CHANNEL_TOKEN_ENV)
    timeout_text = os.environ.get(RESPONSE_TIMEOUT_ENV)
    if not directory or not token or not timeout_text:
        raise RuntimeError("Teamwork MCP 代理缺少临时通道环境变量")
    try:
        timeout_seconds = float(timeout_text)
    except ValueError as exc:
        raise RuntimeError("Teamwork MCP 响应超时配置无效") from exc
    if timeout_seconds <= 0:
        raise RuntimeError("Teamwork MCP 响应超时必须大于零")
    return McpBridgeChannel(
        directory=Path(directory).expanduser().resolve(),
        token=token,
        response_timeout_seconds=timeout_seconds,
    )


async def _handle_request(
    channel: McpBridgeChannel,
    claimed_path: Path,
    request_id: str,
) -> None:
    """验证单个请求，并调用现有 MCP 权限校验实现。"""

    response_path = channel.responses_directory / f"{request_id}.response.json"
    response: dict[str, Any] = {
        "version": CHANNEL_VERSION,
        "request_id": request_id,
        "token": channel.token,
        "ok": False,
    }
    try:
        request = _read_json_object(claimed_path)
        if request.get("version") != CHANNEL_VERSION:
            raise ValueError("MCP Bridge 协议版本不匹配")
        if request.get("request_id") != request_id:
            raise ValueError("MCP Bridge 请求 ID 不匹配")
        if request.get("token") != channel.token:
            raise PermissionError("MCP Bridge 通道令牌无效")
        if request.get("method") != "invoke_agent":
            raise PermissionError("MCP Bridge 只允许 invoke_agent")
        params = request.get("params")
        if not isinstance(params, dict):
            raise ValueError("invoke_agent 参数格式无效")
        agent_name = params.get("agent_name")
        task = params.get("task")
        extra_context = params.get("extra_context")
        if not isinstance(agent_name, str) or not agent_name.strip():
            raise ValueError("invoke_agent 缺少 agent_name")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("invoke_agent 缺少 task")
        if extra_context is not None and not isinstance(extra_context, dict):
            raise ValueError("invoke_agent 的 extra_context 必须是对象")

        # 延迟导入可避免 CodexRunner 与桥接模块在主服务进程中形成循环依赖。
        from .mcp_server import invoke_agent

        result = await invoke_agent(agent_name, task, extra_context)
        response.update({"ok": True, "result": result})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        response["error"] = {
            "type": type(exc).__name__,
            "message": str(exc) or type(exc).__name__,
        }
    finally:
        with suppress(OSError):
            claimed_path.unlink()
    _atomic_write_json(response_path, response)


def _request_current_run_cancellation() -> None:
    """把代理中断转换为当前 Agent 及其后代的持久化取消。"""

    config_path = os.environ.get("TEAMWORK_CONFIG_PATH")
    encoded_context = os.environ.get("TEAMWORK_INVOCATION_CONTEXT")
    if not config_path or not encoded_context:
        raise RuntimeError("MCP Broker 缺少 Teamwork 配置或调用上下文")

    # 延迟导入可避免主服务加载 CodexRunner 时形成循环依赖。
    from .codex_runner import decode_invocation_context
    from .config import load_config
    from .state import CANCEL_SOURCE_ADMINISTRATOR, StateStore

    config = load_config(config_path)
    context = decode_invocation_context(encoded_context)
    store = StateStore(config.database.path)
    store.initialize()
    source = store.agent_run_cancel_source(context.run_id)
    store.request_cancel_run(
        context.run_id,
        source=source or CANCEL_SOURCE_ADMINISTRATOR,
    )


async def run_broker() -> None:
    """持续认领请求；停止时先持久化取消，再等待委托自行收尾。"""

    channel = channel_from_environment()
    tasks: dict[str, asyncio.Task[None]] = {}
    stopping = False
    try:
        for directory in (
            channel.requests_directory,
            channel.processing_directory,
            channel.responses_directory,
            channel.cancellations_directory,
        ):
            if not directory.is_dir():
                raise RuntimeError(f"MCP Bridge 通道目录不存在：{directory.name}")
        if not os.environ.get("TEAMWORK_CONFIG_PATH") or not os.environ.get(
            "TEAMWORK_INVOCATION_CONTEXT"
        ):
            raise RuntimeError("MCP Broker 缺少 Teamwork 配置或调用上下文")
        _atomic_write_json(
            channel.ready_path,
            {"version": CHANNEL_VERSION, "ready": True},
        )
        while True:
            if channel.stop_path.exists() and not stopping:
                stopping = True
                if tasks:
                    await asyncio.to_thread(_request_current_run_cancellation)
            for cancellation_path in channel.cancellations_directory.glob(
                "*.cancel.json"
            ):
                with suppress(OSError):
                    cancellation_path.unlink()
                await asyncio.to_thread(_request_current_run_cancellation)
            if not stopping:
                for request_path in channel.requests_directory.glob(
                    "*.request.json"
                ):
                    request_id = request_path.name.removesuffix(".request.json")
                    if not _valid_request_id(request_id) or request_id in tasks:
                        with suppress(OSError):
                            request_path.unlink()
                        continue
                    claimed_path = (
                        channel.processing_directory / f"{request_id}.json"
                    )
                    try:
                        os.replace(request_path, claimed_path)
                    except FileNotFoundError:
                        continue
                    tasks[request_id] = asyncio.create_task(
                        _handle_request(channel, claimed_path, request_id)
                    )
            finished = [request_id for request_id, task in tasks.items() if task.done()]
            for request_id in finished:
                task = tasks.pop(request_id)
                with suppress(asyncio.CancelledError, Exception):
                    await task
            if stopping and not tasks:
                break
            await asyncio.sleep(_BROKER_POLL_SECONDS)
    except Exception as exc:
        with suppress(OSError):
            _atomic_write_json(
                channel.error_path,
                {"version": CHANNEL_VERSION, "message": str(exc)},
            )
        raise
    finally:
        for task in tasks.values():
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)


def main() -> None:
    """运行只供 Teamwork 内部启动的 Broker 子进程入口。"""

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("mode", choices=("broker",))
    arguments = parser.parse_args()
    if arguments.mode == "broker":
        asyncio.run(run_broker())


if __name__ == "__main__":
    main()
