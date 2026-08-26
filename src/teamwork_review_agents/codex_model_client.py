"""进程内 Codex OAuth 与 Responses SSE 客户端。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from .subprocess_utils import resolve_executable


CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_REFRESH_SKEW_MS = 60_000
RETRYABLE_HTTP_STATUS = {502, 503, 504}
_UPSTREAM_ERROR_MAX_CHARS = 2000
_UPSTREAM_ERROR_FIELD_MAX_CHARS = 500
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
_OAUTH_LOCKS: dict[str, asyncio.Lock] = {}
_OAUTH_LOCKS_GUARD = threading.Lock()


class CodexModelError(RuntimeError):
    """表示内嵌 Codex 模型客户端无法继续执行。"""


class CodexOAuthError(CodexModelError):
    """表示已有 Codex OAuth 登录状态不可用。"""


class CodexUpstreamError(CodexModelError):
    """表示 Codex 上游响应失败或协议不完整。"""

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


@dataclass(frozen=True)
class CodexOAuthCredentials:
    """一次 Codex 上游请求需要的 OAuth 凭据。"""

    access_token: str
    refresh_token: str
    expires_at_ms: int
    account_id: str | None = None
    id_token: str | None = None


class CodexOAuthStore:
    """只复用既有 Codex 登录，并在必要时原子刷新 token。"""

    def __init__(
        self,
        codex_home: Path,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.codex_home = codex_home.expanduser().resolve()
        self.auth_path = self.codex_home / "auth.json"
        self._lock = _oauth_lock(self.auth_path)
        self.transport = transport

    def load(self) -> CodexOAuthCredentials | None:
        """读取 Codex CLI 的 ChatGPT OAuth 凭据。"""

        try:
            document = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(document, dict) or document.get("auth_mode") != "chatgpt":
            return None
        tokens = document.get("tokens")
        if not isinstance(tokens, dict):
            return None
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token")
        if not isinstance(access, str) or not access.strip():
            return None
        if not isinstance(refresh, str) or not refresh.strip():
            return None
        expires = document.get("expires")
        expires_at_ms = (
            int(expires)
            if isinstance(expires, (int, float))
            else _jwt_expiry_ms(access) or 0
        )
        account_id = tokens.get("account_id")
        id_token = tokens.get("id_token")
        return CodexOAuthCredentials(
            access_token=access.strip(),
            refresh_token=refresh.strip(),
            expires_at_ms=expires_at_ms,
            account_id=(
                account_id.strip()
                if isinstance(account_id, str) and account_id.strip()
                else _jwt_account_id(access)
            ),
            id_token=id_token if isinstance(id_token, str) and id_token else None,
        )

    async def credentials(self, *, force_refresh: bool = False) -> CodexOAuthCredentials:
        """返回可用凭据；临近过期时通过 OAuth refresh token 更新。"""

        async with self._lock:
            credentials = self.load()
            if credentials is None:
                raise CodexOAuthError(
                    f"Codex 模型基座模式需要在 {self.auth_path} 完成 ChatGPT OAuth 登录"
                )
            if not force_refresh and not _expired_or_near(credentials):
                return credentials
            try:
                refreshed = await self._refresh(credentials.refresh_token)
            except CodexOAuthError:
                # Codex CLI 可能在本进程刷新期间轮换了 refresh token；优先接收新文件。
                latest = self.load()
                if latest is not None and latest.access_token != credentials.access_token:
                    return latest
                raise
            self._save(refreshed)
            return refreshed

    async def _refresh(self, refresh_token: str) -> CodexOAuthCredentials:
        """刷新过期 token，错误只保留状态码和安全类型。"""

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=30.0),
                transport=self.transport,
            ) as client:
                response = await client.post(
                    OAUTH_TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": OAUTH_CLIENT_ID,
                    },
                )
        except httpx.HTTPError as exc:
            raise CodexOAuthError(
                f"刷新 Codex OAuth 登录失败：{type(exc).__name__}"
            ) from exc
        if response.status_code >= 400:
            raise CodexOAuthError(
                f"刷新 Codex OAuth 登录失败（HTTP {response.status_code}），请重新登录 Codex"
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise CodexOAuthError("刷新 Codex OAuth 登录时收到无效 JSON") from exc
        if not isinstance(payload, dict):
            raise CodexOAuthError("刷新 Codex OAuth 登录时收到无效响应")
        access = payload.get("access_token")
        refreshed = payload.get("refresh_token") or refresh_token
        expires_in = payload.get("expires_in")
        if not isinstance(access, str) or not access:
            raise CodexOAuthError("Codex OAuth 刷新响应缺少 access_token")
        if not isinstance(refreshed, str) or not refreshed:
            raise CodexOAuthError("Codex OAuth 刷新响应缺少 refresh_token")
        expires_at_ms = (
            _now_ms() + int(expires_in) * 1000
            if isinstance(expires_in, (int, float))
            else _jwt_expiry_ms(access) or 0
        )
        id_token = payload.get("id_token")
        return CodexOAuthCredentials(
            access_token=access,
            refresh_token=refreshed,
            expires_at_ms=expires_at_ms,
            account_id=_jwt_account_id(access),
            id_token=id_token if isinstance(id_token, str) and id_token else None,
        )

    def _save(self, credentials: CodexOAuthCredentials) -> None:
        """保留 Codex auth.json 的未知字段，并以当前用户权限原子替换。"""

        try:
            existing = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing_tokens = existing.get("tokens")
        if not isinstance(existing_tokens, dict):
            existing_tokens = {}
        document = {
            **existing,
            "auth_mode": "chatgpt",
            "tokens": {
                **existing_tokens,
                "access_token": credentials.access_token,
                "refresh_token": credentials.refresh_token,
                **(
                    {"account_id": credentials.account_id}
                    if credentials.account_id
                    else {}
                ),
                **({"id_token": credentials.id_token} if credentials.id_token else {}),
            },
            "expires": credentials.expires_at_ms,
            "last_refresh": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        self.auth_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.auth_path.with_name(
            f".{self.auth_path.name}.teamwork-{os.getpid()}-{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                temporary.chmod(0o600)
            except OSError:
                # 原生 Windows 不完整支持 POSIX mode，仍由当前用户创建文件。
                pass
            os.replace(temporary, self.auth_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class CodexResponsesClient:
    """直接访问 Codex Responses SSE，不经过本地 API 服务。"""

    def __init__(
        self,
        *,
        oauth: CodexOAuthStore,
        codex_binary: str,
        timeout_seconds: float = 1200.0,
        idle_timeout_seconds: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.oauth = oauth
        self.codex_binary = codex_binary
        self.timeout_seconds = timeout_seconds
        self.idle_timeout_seconds = idle_timeout_seconds
        self.transport = transport

    async def create_response(
        self,
        payload: dict[str, Any],
        *,
        event_callback: EventCallback | None = None,
    ) -> dict[str, Any]:
        """发送一次请求并返回 completed response。"""

        last_error: Exception | None = None
        for attempt in range(3):
            emitted = False
            try:
                response: dict[str, Any] | None = None
                text_deltas: list[str] = []
                output_items: list[dict[str, Any]] = []
                output_item_ids: set[str] = set()
                async for event in self._stream_once(payload):
                    emitted = True
                    if event_callback is not None:
                        await event_callback(event)
                    event_type = event.get("type")
                    if event_type == "response.output_text.delta" and isinstance(
                        event.get("delta"), str
                    ):
                        text_deltas.append(event["delta"])
                    if event_type == "response.output_item.done" and isinstance(
                        event.get("item"), dict
                    ):
                        item = event["item"]
                        item_id = str(item.get("id") or "")
                        if not item_id or item_id not in output_item_ids:
                            output_items.append(item)
                            if item_id:
                                output_item_ids.add(item_id)
                    if event_type == "response.completed" and isinstance(
                        event.get("response"), dict
                    ):
                        response = event["response"]
                    elif event_type in {"response.failed", "error"}:
                        raise CodexUpstreamError(
                            _format_upstream_error(
                                event_type=event_type,
                                payload=event,
                            ),
                            fallbackable=_upstream_error_fallbackable(event),
                        )
                if response is None:
                    raise CodexUpstreamError("Codex SSE 在 completed 事件前结束")
                aggregated = dict(response)
                if output_items and not aggregated.get("output"):
                    aggregated["output"] = output_items
                if text_deltas and not aggregated.get("output_text"):
                    aggregated["output_text"] = "".join(text_deltas)
                return aggregated
            except Exception as exc:
                last_error = exc
                retryable = (
                    not emitted
                    and attempt < 2
                    and _retryable_error(exc)
                )
                if not retryable:
                    raise
                await asyncio.sleep(2**attempt)
        assert last_error is not None
        raise last_error

    async def _stream_once(self, payload: dict[str, Any]):
        """执行单次 SSE 请求，401 时重新吸收一次宿主登录状态。"""

        request_payload = dict(payload)
        request_payload["stream"] = True
        credentials = await self.oauth.credentials()
        for auth_attempt in range(2):
            timeout = httpx.Timeout(
                self.timeout_seconds,
                connect=30.0,
                read=self.idle_timeout_seconds,
            )
            try:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    transport=self.transport,
                ) as client:
                    async with client.stream(
                        "POST",
                        CODEX_RESPONSES_URL,
                        headers=_codex_headers(
                            credentials,
                            codex_binary=self.codex_binary,
                        ),
                        json=request_payload,
                    ) as response:
                        if response.status_code >= 400:
                            body = await response.aread()
                            if response.status_code == 401 and auth_attempt == 0:
                                latest = self.oauth.load()
                                credentials = await self.oauth.credentials(
                                    force_refresh=(
                                        latest is None
                                        or latest.access_token == credentials.access_token
                                    )
                                )
                                continue
                            raise CodexUpstreamError(
                                _format_upstream_error(
                                    payload=_decode_error_body(body),
                                    status_code=response.status_code,
                                ),
                                status_code=response.status_code,
                                fallbackable=response.status_code
                                in {401, 402, 403, 404, 408, 409, 429}
                                or response.status_code >= 500,
                            )
                        content_type = response.headers.get("content-type", "")
                        if "text/event-stream" not in content_type:
                            body = (await response.aread()).decode(
                                "utf-8", errors="replace"
                            )
                            buffered_events = _buffered_sse_events(body)
                            if buffered_events:
                                for event in buffered_events:
                                    yield event
                                return
                            try:
                                document = json.loads(body)
                            except json.JSONDecodeError as exc:
                                raise CodexUpstreamError(
                                    "Codex 上游返回了非 SSE 且非 JSON 的响应",
                                    fallbackable=False,
                                ) from exc
                            if not isinstance(document, dict):
                                raise CodexUpstreamError(
                                    "Codex 上游 JSON 响应格式无效",
                                    fallbackable=False,
                                )
                            if _contains_upstream_error(document):
                                raise CodexUpstreamError(
                                    _format_upstream_error(payload=document),
                                    fallbackable=_upstream_error_fallbackable(document),
                                )
                            yield {"type": "response.completed", "response": document}
                            return
                        async for line in response.aiter_lines():
                            stripped = line.strip()
                            if not stripped.startswith("data:"):
                                continue
                            data = stripped[5:].strip()
                            if data == "[DONE]":
                                return
                            if not data:
                                continue
                            try:
                                event = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(event, dict):
                                yield event
                                if event.get("type") == "response.completed":
                                    return
                        return
            except httpx.HTTPError as exc:
                raise CodexUpstreamError(
                    f"Codex 上游连接失败：{type(exc).__name__}",
                    fallbackable=True,
                ) from exc


def _codex_headers(
    credentials: CodexOAuthCredentials,
    *,
    codex_binary: str,
) -> dict[str, str]:
    """构造与 Codex CLI 能力协商兼容的安全请求头。"""

    version = _codex_client_version(codex_binary)
    headers = {
        "Authorization": f"Bearer {credentials.access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "originator": "teamwork-review-agents",
        "version": version,
        "User-Agent": f"teamwork-review-agents/{version}",
    }
    if credentials.account_id:
        headers["ChatGPT-Account-Id"] = credentials.account_id
    return headers


def _oauth_lock(path: Path) -> asyncio.Lock:
    """让同一服务进程内共享 auth.json 的并发刷新串行。"""

    key = str(path)
    with _OAUTH_LOCKS_GUARD:
        lock = _OAUTH_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _OAUTH_LOCKS[key] = lock
        return lock


@lru_cache(maxsize=16)
def _codex_client_version(codex_binary: str) -> str:
    """读取本机 Codex CLI 版本供上游能力协商。"""

    try:
        completed = subprocess.run(
            [resolve_executable(codex_binary), "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", f"{completed.stdout}\n{completed.stderr}")
    return match.group(1) if match else "unknown"


def _retryable_error(error: Exception) -> bool:
    """只在尚无任何事件时重试短暂网络和网关失败。"""

    if isinstance(error, CodexUpstreamError):
        return error.status_code in RETRYABLE_HTTP_STATUS
    cause = error.__cause__
    return isinstance(
        cause,
        (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            httpx.ReadError,
        ),
    )


def _decode_error_body(body: bytes) -> Any:
    """解析 HTTP 错误正文，无法解析时仅保留原始文本供有界脱敏。"""

    text = body.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _contains_upstream_error(document: dict[str, Any]) -> bool:
    """判断非 SSE JSON 是否是上游错误，而不是正常 completed 响应。"""

    candidates: list[Any] = [document]
    while candidates:
        candidate = candidates.pop()
        if not isinstance(candidate, dict):
            continue
        status = candidate.get("status")
        event_type = candidate.get("type")
        if any(
            isinstance(candidate.get(key), (dict, list, str))
            for key in ("error", "errors")
        ):
            return True
        if isinstance(status, str) and status in {"failed", "error"}:
            return True
        if isinstance(event_type, str) and event_type in {
            "error",
            "response.failed",
        }:
            return True
        nested_response = candidate.get("response")
        if isinstance(nested_response, dict):
            candidates.append(nested_response)
    return False


def _format_upstream_error(
    *,
    payload: Any,
    event_type: str | None = None,
    status_code: int | None = None,
) -> str:
    """提取上游有限错误字段并生成可写入运行记录的脱敏文本。"""

    fields = _extract_upstream_error_fields(payload)
    if status_code is not None:
        prefix = f"Codex 上游请求失败（HTTP {status_code}）"
    elif event_type in {"response.failed", "error"}:
        prefix = "Codex 上游报告模型回合失败"
    else:
        prefix = "Codex 上游返回错误"

    message = fields.get("message")
    if not message:
        message = "上游未提供具体错误消息"
    details = [
        f"类型={fields['type']}" if fields.get("type") else "",
        f"代码={fields['code']}" if fields.get("code") else "",
        f"参数={fields['param']}" if fields.get("param") else "",
        f"请求 ID={fields['request_id']}" if fields.get("request_id") else "",
    ]
    suffix = f"（{'，'.join(item for item in details if item)}）" if any(details) else ""
    text = f"{prefix}：{message}{suffix}"
    return _sanitize_upstream_text(text, limit=_UPSTREAM_ERROR_MAX_CHARS)


def _upstream_error_fallbackable(payload: Any) -> bool:
    """区分模型服务不可用与请求内容本身无效的上游错误。"""

    fields = _extract_upstream_error_fields(payload)
    markers = " ".join(
        fields.get(name, "")
        for name in ("type", "code", "message")
    ).lower()
    non_fallbackable_markers = (
        "invalid_request",
        "invalid prompt",
        "context_length",
        "context window",
        "unsupported parameter",
        "tool schema",
        "validation_error",
        "malformed",
    )
    if any(marker in markers for marker in non_fallbackable_markers):
        return False
    return True


def _extract_upstream_error_fields(payload: Any) -> dict[str, str]:
    """只读取错误对象中的白名单字段，避免持久化完整上游载荷。"""

    aliases = {
        "message": "message",
        "detail": "message",
        "error_description": "message",
        "reason": "message",
        "failure_reason": "message",
        "type": "type",
        "error_type": "type",
        "code": "code",
        "error_code": "code",
        "param": "param",
        "parameter": "param",
        "request_id": "request_id",
        "requestid": "request_id",
        "x_request_id": "request_id",
        "x-request-id": "request_id",
    }
    fields: dict[str, str] = {}
    queue: list[Any] = [payload]
    visited: set[int] = set()
    inspected = 0
    while queue and inspected < 32:
        candidate = queue.pop(0)
        inspected += 1
        if isinstance(candidate, str):
            if "message" not in fields:
                fields["message"] = _sanitize_upstream_text(
                    candidate,
                    limit=_UPSTREAM_ERROR_FIELD_MAX_CHARS,
                )
            continue
        if not isinstance(candidate, (dict, list)):
            continue
        identity = id(candidate)
        if identity in visited:
            continue
        visited.add(identity)
        if isinstance(candidate, list):
            queue.extend(candidate[:8])
            continue
        for key, value in candidate.items():
            normalized = str(key).lower().replace(" ", "_")
            field = aliases.get(normalized)
            if field and field not in fields:
                if field == "type" and str(value) in {
                    "response.failed",
                    "error",
                }:
                    continue
                sanitized = _sanitize_upstream_text(
                    value,
                    limit=_UPSTREAM_ERROR_FIELD_MAX_CHARS,
                )
                if sanitized:
                    fields[field] = sanitized
            if str(key).lower() in {
                "error",
                "errors",
                "response",
                "details",
                "status_details",
                "incomplete_details",
                "data",
            }:
                queue.append(value)
    return fields


def _sanitize_upstream_text(value: Any, *, limit: int) -> str:
    """清除控制字符、常见凭据格式并限制上游错误文本长度。"""

    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return ""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(
        r"(?i)\bBearer\s+[^\s,;，；（）()]+",
        "Bearer [已脱敏]",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}",
        "[已脱敏]",
        text,
    )
    text = re.sub(
        r"(?i)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|authorization)"
        r"\s*[:=]\s*[^\s,;，；（）()]+",
        r"\1=[已脱敏]",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:api[_-]?key|token|access_token|refresh_token)=)[^&#\s]+",
        r"\1[已脱敏]",
        text,
    )
    if len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def _buffered_sse_events(body: str) -> list[dict[str, Any]]:
    """兼容漏写 content-type 但正文仍为标准 SSE 的上游响应。"""

    events: list[dict[str, Any]] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        data = stripped[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """只解码本地 JWT payload，不验证或记录 token。"""

    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        document = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}
    return document if isinstance(document, dict) else {}


def _jwt_expiry_ms(token: str) -> int | None:
    """读取 JWT 过期时间并转为毫秒。"""

    expires = _decode_jwt_payload(token).get("exp")
    return int(float(expires) * 1000) if isinstance(expires, (int, float)) else None


def _jwt_account_id(token: str) -> str | None:
    """读取 ChatGPT account id。"""

    claim = _decode_jwt_payload(token).get("https://api.openai.com/auth")
    if not isinstance(claim, dict):
        return None
    account_id = claim.get("chatgpt_account_id")
    return account_id if isinstance(account_id, str) and account_id else None


def _expired_or_near(credentials: CodexOAuthCredentials) -> bool:
    """判断 access token 是否应立即刷新。"""

    return bool(
        credentials.expires_at_ms
        and credentials.expires_at_ms <= _now_ms() + TOKEN_REFRESH_SKEW_MS
    )


def _now_ms() -> int:
    """返回当前毫秒时间戳。"""

    return int(time.time() * 1000)
