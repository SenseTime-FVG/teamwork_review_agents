"""外部模型 Provider 的协议适配与模型目录发现。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx

from .config import ModelProviderConfig


EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class ModelResponseClient(Protocol):
    """Teamwork 模型工具循环使用的统一响应客户端。"""

    async def create_response(
        self,
        payload: dict[str, Any],
        *,
        event_callback: EventCallback | None = None,
    ) -> dict[str, Any]: ...


class ModelProviderRequestError(RuntimeError):
    """表示外部模型 Provider 请求失败，并保留可安全判断的状态信息。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        fallbackable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.fallbackable = fallbackable


class ExternalModelClient:
    """把四类外部 API 响应规范化为 Responses 工具循环格式。"""

    def __init__(
        self,
        provider: ModelProviderConfig,
        api_key: str,
        *,
        timeout_seconds: float,
        idle_timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.idle_timeout_seconds = idle_timeout_seconds
        self.transport = transport

    async def create_response(
        self,
        payload: dict[str, Any],
        *,
        event_callback: EventCallback | None = None,
    ) -> dict[str, Any]:
        """调用对应原生协议并返回统一响应。"""

        driver = self.provider.driver
        if driver == "openai_responses":
            response = await self._openai_responses(payload)
        elif driver == "openai_chat_completions":
            response = await self._openai_chat(payload)
        elif driver == "anthropic_messages":
            response = await self._anthropic_messages(payload)
        elif driver == "gemini_generate_content":
            response = await self._gemini_generate_content(payload)
        else:
            raise ModelProviderRequestError(f"不支持的外部模型协议：{driver}")
        if event_callback is not None:
            await _emit_normalized_events(response, event_callback)
        return response

    async def _openai_responses(self, payload: dict[str, Any]) -> dict[str, Any]:
        """调用 OpenAI Responses 或兼容接口。"""

        request_payload = dict(payload)
        request_payload["stream"] = False
        document = await self._post_json(
            _api_url(self.provider, "/v1/responses"),
            request_payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        if not isinstance(document.get("output"), list):
            raise ModelProviderRequestError("Responses 接口返回内容缺少 output")
        return document

    async def _openai_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把 Responses 历史转换为 Chat Completions。"""

        body: dict[str, Any] = {
            "model": payload["model"],
            "messages": _chat_messages(
                str(payload.get("instructions") or ""),
                payload.get("input"),
            ),
            "stream": False,
        }
        tools = [_chat_tool(item) for item in payload.get("tools", [])]
        if tools:
            body["tools"] = tools
            body["tool_choice"] = payload.get("tool_choice") or "auto"
        reasoning = payload.get("reasoning")
        if isinstance(reasoning, dict) and reasoning.get("effort"):
            body["reasoning_effort"] = reasoning["effort"]
        document = await self._post_json(
            _api_url(self.provider, "/v1/chat/completions"),
            body,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        choices = document.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelProviderRequestError("Chat Completions 返回内容缺少 choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise ModelProviderRequestError("Chat Completions 返回消息格式无效")
        output: list[dict[str, Any]] = []
        text = _text_content(message.get("content"))
        if text:
            output.append(_message_item(text))
        for raw_call in message.get("tool_calls", []):
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict):
                continue
            call_id = str(raw_call.get("id") or uuid.uuid4().hex)
            output.append(
                {
                    "type": "function_call",
                    "id": call_id,
                    "call_id": call_id,
                    "name": str(function.get("name") or ""),
                    "arguments": str(function.get("arguments") or "{}"),
                }
            )
        return {
            "id": str(document.get("id") or uuid.uuid4().hex),
            "output": output,
            "output_text": text,
            "usage": _openai_usage(document.get("usage")),
        }

    async def _anthropic_messages(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把统一历史转换为 Anthropic Messages。"""

        body: dict[str, Any] = {
            "model": payload["model"],
            "system": str(payload.get("instructions") or ""),
            "messages": _anthropic_messages(payload.get("input")),
            "max_tokens": 16384,
            "stream": False,
        }
        tools = [_anthropic_tool(item) for item in payload.get("tools", [])]
        if tools:
            body["tools"] = tools
        document = await self._post_json(
            _api_url(self.provider, "/v1/messages"),
            body,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        content = document.get("content")
        if not isinstance(content, list):
            raise ModelProviderRequestError("Anthropic 返回内容缺少 content")
        output: list[dict[str, Any]] = []
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
                output.append(_message_item(block["text"]))
            elif block.get("type") == "tool_use":
                call_id = str(block.get("id") or uuid.uuid4().hex)
                output.append(
                    {
                        "type": "function_call",
                        "id": call_id,
                        "call_id": call_id,
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    }
                )
        usage = document.get("usage") if isinstance(document.get("usage"), dict) else {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        return {
            "id": str(document.get("id") or uuid.uuid4().hex),
            "output": output,
            "output_text": "".join(texts),
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        }

    async def _gemini_generate_content(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把统一历史转换为 Gemini GenerateContent。"""

        model = str(payload["model"])
        body: dict[str, Any] = {
            "systemInstruction": {
                "parts": [{"text": str(payload.get("instructions") or "")}],
            },
            "contents": _gemini_contents(payload.get("input")),
        }
        declarations = [_gemini_tool(item) for item in payload.get("tools", [])]
        if declarations:
            body["tools"] = [{"functionDeclarations": declarations}]
        url = _gemini_url(self.provider, model)
        document = await self._post_json(
            url,
            body,
            headers={"x-goog-api-key": self.api_key},
        )
        candidates = document.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ModelProviderRequestError("Gemini 返回内容缺少 candidates")
        content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise ModelProviderRequestError("Gemini 返回消息格式无效")
        output: list[dict[str, Any]] = []
        texts: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
                output.append(_message_item(part["text"]))
            function = part.get("functionCall")
            if isinstance(function, dict):
                call_id = str(function.get("id") or uuid.uuid4().hex)
                output.append(
                    {
                        "type": "function_call",
                        "id": call_id,
                        "call_id": call_id,
                        "name": str(function.get("name") or ""),
                        "arguments": json.dumps(function.get("args") or {}, ensure_ascii=False),
                    }
                )
        metadata = document.get("usageMetadata")
        usage = metadata if isinstance(metadata, dict) else {}
        input_tokens = int(usage.get("promptTokenCount") or 0)
        output_tokens = int(usage.get("candidatesTokenCount") or 0)
        return {
            "id": uuid.uuid4().hex,
            "output": output,
            "output_text": "".join(texts),
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": int(usage.get("totalTokenCount") or input_tokens + output_tokens),
            },
        }

    async def _post_json(
        self,
        url: str,
        body: dict[str, Any],
        *,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """发送一次不记录正文的 JSON 请求。"""

        timeout = httpx.Timeout(
            self.timeout_seconds,
            connect=min(30.0, self.timeout_seconds),
            read=self.idle_timeout_seconds,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                response = await client.post(
                    url,
                    headers={"Content-Type": "application/json", **headers},
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise ModelProviderRequestError(
                f"模型 Provider 连接失败：{type(exc).__name__}",
                fallbackable=True,
            ) from exc
        if response.status_code >= 400:
            raise ModelProviderRequestError(
                f"模型 Provider 请求失败（HTTP {response.status_code}）",
                status_code=response.status_code,
                fallbackable=response.status_code
                in {401, 402, 403, 404, 408, 409, 429}
                or response.status_code >= 500,
            )
        try:
            document = response.json()
        except ValueError as exc:
            raise ModelProviderRequestError("模型 Provider 返回了无效 JSON") from exc
        if not isinstance(document, dict):
            raise ModelProviderRequestError("模型 Provider JSON 顶层不是对象")
        return document


async def discover_provider_models(
    provider: ModelProviderConfig,
    api_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """按协议读取模型目录；不支持时由调用方保留手工模型。"""

    if provider.driver == "codex_cli":
        return list(provider.models)
    return await discover_model_catalog(
        driver=provider.driver,
        base_url=str(provider.base_url or ""),
        api_key=api_key,
        timeout_seconds=float(provider.request_timeout_seconds),
        transport=transport,
    )


async def discover_model_catalog(
    *,
    driver: Literal[
        "openai_responses",
        "openai_chat_completions",
        "anthropic_messages",
        "gemini_generate_content",
    ],
    base_url: str,
    api_key: str,
    timeout_seconds: float = 120.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """使用未保存的 Provider 草稿参数读取模型目录。"""

    if driver == "gemini_generate_content":
        url = _join_api_url(base_url, "/v1beta/models")
        headers = {"x-goog-api-key": api_key}
    else:
        url = _join_api_url(base_url, "/v1/models")
        headers = (
            {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
            if driver == "anthropic_messages"
            else {"Authorization": f"Bearer {api_key}"}
        )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(float(timeout_seconds), connect=30.0),
            transport=transport,
        ) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ModelProviderRequestError(
            f"模型目录连接失败：{type(exc).__name__}"
        ) from exc
    if response.status_code >= 400:
        raise ModelProviderRequestError(f"模型目录请求失败（HTTP {response.status_code}）")
    try:
        document = response.json()
    except ValueError as exc:
        raise ModelProviderRequestError("模型目录返回了无效 JSON") from exc
    raw_models = document.get("models") if driver == "gemini_generate_content" else document.get("data")
    if not isinstance(raw_models, list):
        raise ModelProviderRequestError("模型目录响应缺少模型数组")
    result: list[str] = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        model = item.get("name") if driver == "gemini_generate_content" else item.get("id")
        if isinstance(model, str) and model:
            result.append(model.removeprefix("models/"))
    return sorted(set(result))


async def _emit_normalized_events(
    response: dict[str, Any],
    callback: EventCallback,
) -> None:
    """让非 Responses 协议仍产生一致的线程与消息事件。"""

    response_id = str(response.get("id") or uuid.uuid4().hex)
    await callback({"type": "response.created", "response": {"id": response_id}})
    for item in response.get("output", []):
        if isinstance(item, dict):
            await callback({"type": "response.output_item.done", "item": item})
    await callback({"type": "response.completed", "response": response})


def _api_url(provider: ModelProviderConfig, path: str) -> str:
    """避免 Base URL 已含版本段时重复拼接。"""

    return _join_api_url(str(provider.base_url or ""), path)


def _join_api_url(base_url: str, path: str) -> str:
    """拼接模型 API 地址并避免版本段重复。"""

    root = base_url.rstrip("/")
    if root.endswith("/v1") and path.startswith("/v1/"):
        return f"{root}{path[3:]}"
    if root.endswith("/v1beta") and path.startswith("/v1beta/"):
        return f"{root}{path[7:]}"
    return f"{root}{path}"


def _gemini_url(provider: ModelProviderConfig, model: str) -> str:
    """生成 Gemini 模型调用路径。"""

    return _api_url(provider, f"/v1beta/models/{quote(model, safe='-._')}:generateContent")


def _message_item(text: str) -> dict[str, Any]:
    """构造 Responses 风格的助手消息。"""

    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _text_content(value: Any) -> str:
    """从字符串或内容数组提取纯文本。"""

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(item.get("text") or "")
            for item in value
            if isinstance(item, dict)
        )
    return ""


def _history_text(item: dict[str, Any]) -> str:
    """读取统一历史条目的文本。"""

    return _text_content(item.get("content"))


def _chat_messages(instructions: str, raw_history: Any) -> list[dict[str, Any]]:
    """生成 Chat Completions 消息数组。"""

    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    history = raw_history if isinstance(raw_history, list) else []
    index = 0
    while index < len(history):
        item = history[index]
        if not isinstance(item, dict):
            index += 1
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            tool_calls: list[dict[str, Any]] = []
            while index < len(history):
                call = history[index]
                if not isinstance(call, dict) or call.get("type") != "function_call":
                    break
                call_id = str(
                    call.get("call_id") or call.get("id") or uuid.uuid4().hex
                )
                tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": str(call.get("name") or ""),
                            "arguments": str(call.get("arguments") or "{}"),
                        },
                    }
                )
                index += 1
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                }
            )
            continue
        elif item_type == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or ""),
                    "content": str(item.get("output") or ""),
                }
            )
        else:
            role = "assistant" if item.get("role") == "assistant" else "user"
            text = _history_text(item)
            if text:
                messages.append({"role": role, "content": text})
        index += 1
    return messages


def _chat_tool(item: Any) -> dict[str, Any]:
    """转换 Responses 函数定义。"""

    value = item if isinstance(item, dict) else {}
    return {
        "type": "function",
        "function": {
            "name": value.get("name"),
            "description": value.get("description", ""),
            "parameters": value.get("parameters", {"type": "object"}),
        },
    }


def _anthropic_messages(raw_history: Any) -> list[dict[str, Any]]:
    """生成 Anthropic 消息与工具结果块。"""

    result: list[dict[str, Any]] = []
    history = raw_history if isinstance(raw_history, list) else []
    for item in history:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            block = {
                "type": "tool_use",
                "id": str(item.get("call_id") or item.get("id") or uuid.uuid4().hex),
                "name": str(item.get("name") or ""),
                "input": _json_object(item.get("arguments")),
            }
            _append_content_message(result, "assistant", block)
        elif item_type == "function_call_output":
            block = {
                "type": "tool_result",
                "tool_use_id": str(item.get("call_id") or ""),
                "content": str(item.get("output") or ""),
            }
            _append_content_message(result, "user", block)
        else:
            role = "assistant" if item.get("role") == "assistant" else "user"
            text = _history_text(item)
            if text:
                _append_content_message(result, role, {"type": "text", "text": text})
    return result


def _anthropic_tool(item: Any) -> dict[str, Any]:
    """转换 Anthropic 工具定义。"""

    value = item if isinstance(item, dict) else {}
    return {
        "name": value.get("name"),
        "description": value.get("description", ""),
        "input_schema": value.get("parameters", {"type": "object"}),
    }


def _gemini_contents(raw_history: Any) -> list[dict[str, Any]]:
    """生成 Gemini contents，并恢复函数名。"""

    result: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}
    history = raw_history if isinstance(raw_history, list) else []
    for item in history:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or uuid.uuid4().hex)
            name = str(item.get("name") or "")
            call_names[call_id] = name
            _append_gemini_part(
                result,
                "model",
                {"functionCall": {"name": name, "args": _json_object(item.get("arguments"))}},
            )
        elif item_type == "function_call_output":
            call_id = str(item.get("call_id") or "")
            name = call_names.get(call_id, "tool")
            _append_gemini_part(
                result,
                "user",
                {
                    "functionResponse": {
                        "name": name,
                        "response": {"output": str(item.get("output") or "")},
                    }
                },
            )
        else:
            role = "model" if item.get("role") == "assistant" else "user"
            text = _history_text(item)
            if text:
                _append_gemini_part(result, role, {"text": text})
    return result


def _gemini_tool(item: Any) -> dict[str, Any]:
    """转换 Gemini 函数定义。"""

    value = item if isinstance(item, dict) else {}
    return {
        "name": value.get("name"),
        "description": value.get("description", ""),
        "parameters": value.get("parameters", {"type": "object"}),
    }


def _append_content_message(
    messages: list[dict[str, Any]], role: str, block: dict[str, Any]
) -> None:
    """合并相邻同角色的 Anthropic 内容块。"""

    if messages and messages[-1].get("role") == role:
        messages[-1]["content"].append(block)
    else:
        messages.append({"role": role, "content": [block]})


def _append_gemini_part(
    contents: list[dict[str, Any]], role: str, part: dict[str, Any]
) -> None:
    """合并相邻同角色的 Gemini 内容块。"""

    if contents and contents[-1].get("role") == role:
        contents[-1]["parts"].append(part)
    else:
        contents.append({"role": role, "parts": [part]})


def _json_object(value: Any) -> dict[str, Any]:
    """把函数参数规范化为 JSON 对象。"""

    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _openai_usage(value: Any) -> dict[str, Any]:
    """保留 OpenAI usage，并补齐总量。"""

    usage = dict(value) if isinstance(value, dict) else {}
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    return {
        **usage,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens),
    }
