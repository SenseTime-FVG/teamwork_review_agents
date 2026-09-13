"""只使用标准库的沙盒内 STDIO MCP 代理，不导入项目或宿主环境依赖。"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path


_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
_TOOLS = [
    {
        "name": "invoke_agent",
        "description": "调用一个配置允许的 Codex CLI sub-agent，并等待其结构化结果。",
        "inputSchema": {
            "type": "object", "required": ["agent_name", "task"],
            "properties": {"agent_name": {"type": "string"}, "task": {"type": "string"},
                           "extra_context": {"anyOf": [{"type": "object"}, {"type": "null"}], "default": None}},
            "additionalProperties": False,
        },
    },
    {
        "name": "publish_comment",
        "description": "发布或更新当前 Agent 在当前源版本代次的 PR/MR 顶层评论。",
        "inputSchema": {"type": "object", "required": ["body"],
                        "properties": {"body": {"type": "string"}}, "additionalProperties": False},
    },
]


def _write_json(path: Path, value: dict) -> None:
    """沿用 Broker 的同目录原子发布协议，不写入完整环境。"""

    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink()


async def _call_bridge(method: str, params: dict) -> dict:
    """只转发两种固定方法，Broker 继续承担配置、权限与运行上下文校验。"""

    directory = os.environ.get("TEAMWORK_MCP_CHANNEL_DIR", "")
    token = os.environ.get("TEAMWORK_MCP_CHANNEL_TOKEN", "")
    timeout = float(os.environ.get("TEAMWORK_MCP_RESPONSE_TIMEOUT_SECONDS", "3600"))
    if not directory or not token or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Teamwork MCP 通道配置无效")
    root = Path(directory)
    request_id = str(uuid.uuid4())
    envelope = {"version": 1, "request_id": request_id, "token": token}
    _write_json(root / "requests" / f"{request_id}.request.json", {
        **envelope, "method": method, "params": params,
    })
    deadline = time.monotonic() + timeout
    response_path = root / "responses" / f"{request_id}.response.json"
    try:
        while True:
            if response_path.exists():
                response = json.loads(response_path.read_text(encoding="utf-8"))
                with suppress(OSError):
                    response_path.unlink()
                if not isinstance(response, dict) or any(response.get(key) != value for key, value in envelope.items()):
                    raise RuntimeError("Teamwork MCP Broker 响应身份校验失败")
                if response.get("ok") is not True:
                    error = response.get("error")
                    raise RuntimeError(str(error.get("message") or "Broker 调用失败") if isinstance(error, dict) else "Broker 调用失败")
                if not isinstance(response.get("result"), dict):
                    raise RuntimeError("Teamwork MCP Broker 结果格式无效")
                return response["result"]
            if (root / "error.json").exists():
                raise RuntimeError("Teamwork MCP Broker 不可用")
            if time.monotonic() >= deadline:
                raise TimeoutError("等待 Teamwork MCP Broker 响应超时")
            await asyncio.sleep(0.05)
    except (asyncio.CancelledError, TimeoutError):
        # 保持现有 Broker 的取消协议，不能只取消本地等待而留下宿主子任务运行。
        with suppress(OSError):
            _write_json(root / "cancellations" / f"{request_id}.cancel.json", envelope)
        raise


def _tool_arguments(params: dict) -> tuple[str, dict]:
    """拒绝未知工具、错误类型与额外参数，避免无意扩展宿主接口。"""

    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是对象")
    if name == "invoke_agent":
        if set(arguments) - {"agent_name", "task", "extra_context"} or any(
            not isinstance(arguments.get(key), str) for key in ("agent_name", "task")
        ) or (arguments.get("extra_context") is not None and not isinstance(arguments["extra_context"], dict)):
            raise ValueError("invoke_agent 参数无效")
        return name, {"agent_name": arguments["agent_name"], "task": arguments["task"],
                      "extra_context": arguments.get("extra_context")}
    if name == "publish_comment" and set(arguments) == {"body"} and isinstance(arguments["body"], str):
        return name, arguments
    raise ValueError("工具名称或参数无效")


def _send(message: dict) -> None:
    """STDIO 只输出 UTF-8 JSON-RPC，Windows 不依赖控制台编码。"""

    sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


async def _respond(message: dict) -> None:
    """处理 MCP 必需握手、保活和工具请求，业务失败保留工具错误语义。"""

    identifier = message["id"]
    method = message.get("method")
    params = message.get("params", {})
    response = {"jsonrpc": "2.0", "id": identifier}
    if not isinstance(params, dict):
        _send({**response, "error": {"code": -32602, "message": "请求参数必须是对象"}})
        return
    if method == "initialize":
        version = params.get("protocolVersion")
        result = {"protocolVersion": version if version in _VERSIONS else _VERSIONS[-1],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "teamwork-agent-gateway-proxy", "version": "1"},
                  "instructions": "代理仅转发本轮通道请求；工具权限和评论目标均由 Teamwork 服务校验。"}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _TOOLS}
    elif method == "tools/call":
        try:
            name, arguments = _tool_arguments(params)
            value = await _call_bridge(name, arguments)
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                      "structuredContent": value, "isError": False}
        except Exception as exc:
            # 通道令牌和已有 Git 凭据不能因 Broker 异常回显而泄漏到 Agent 消息。
            detail = str(exc)
            for key in ("TEAMWORK_MCP_CHANNEL_TOKEN", "TEAMWORK_GIT_TOKEN"):
                secret = os.environ.get(key)
                if secret:
                    detail = detail.replace(secret, "********")
            result = {"content": [{"type": "text", "text": detail}], "isError": True}
    else:
        _send({**response, "error": {"code": -32601, "message": "未知 MCP 方法"}})
        return
    _send({**response, "result": result})


async def _serve() -> None:
    """并发等待工具结果，继续读取取消通知；输入关闭时取消所有挂起请求。"""

    pending: dict[object, asyncio.Task] = {}
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline, 4 * 1024 * 1024 + 1)
            if not line:
                break
            if len(line) > 4 * 1024 * 1024:
                break
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON 无效"}})
                continue
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "请求格式无效"}})
                continue
            if "id" not in message:
                params = message.get("params")
                if message.get("method") == "notifications/cancelled" and isinstance(params, dict):
                    identifier = params.get("requestId")
                    if isinstance(identifier, (str, int)) and identifier in pending:
                        pending[identifier].cancel()
                continue
            identifier = message["id"]
            if not isinstance(identifier, (str, int)) or isinstance(identifier, bool) or identifier in pending:
                _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "请求 ID 无效或重复"}})
                continue
            task = asyncio.create_task(_respond(message))
            pending[identifier] = task
            task.add_done_callback(lambda finished, key=identifier: pending.pop(key, None))
    finally:
        tasks = list(pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(_serve())
