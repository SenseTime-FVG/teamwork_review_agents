"""Provider 公共接口与工厂。"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Literal

import httpx

from ..config import ProviderConfig, RepositoryConfig, ScannerConfig
from ..models import ChangeRequestActivityBatch, ChangeRequestSnapshot


class ProviderError(RuntimeError):
    """表示代码托管平台请求或数据解析失败。"""


class BaseProvider(ABC):
    """统一 Provider 异步接口。"""

    def __init__(
        self,
        name: str,
        config: ProviderConfig,
        scanner: ScannerConfig,
        *,
        token: str | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self.scanner = scanner
        resolved_token = os.getenv(config.token_env) if token is None else token
        if not resolved_token:
            raise ProviderError(
                f"Provider {name} 缺少 Token：请在全局环境或宿主机环境配置 "
                f"{config.token_env}"
            )
        self.token = resolved_token
        self.client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/",
            timeout=config.request_timeout_seconds,
            headers=self.headers(),
        )

    @abstractmethod
    def headers(self) -> dict[str, str]:
        """返回平台认证和内容协商请求头。"""

    async def close(self) -> None:
        """关闭底层 HTTP 连接池。"""

        await self.client.aclose()

    async def __aenter__(self) -> "BaseProvider":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def get_json_response(
        self,
        path: str,
        **kwargs: object,
    ) -> tuple[object, dict[str, str]]:
        """执行 GET，并同时返回分页所需的响应头。"""

        try:
            response = await self.client.get(path, **kwargs)
            response.raise_for_status()
            return response.json(), dict(response.headers)
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"Provider {self.name} 请求失败：{exc.response.status_code} {path}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"Provider {self.name} 请求失败：{path}：{exc}") from exc

    async def get_json(self, path: str, **kwargs: object) -> object:
        """执行 GET 并将平台错误转换为不泄露凭据的异常。"""

        payload, _ = await self.get_json_response(path, **kwargs)
        return payload

    async def get_optional_json(
        self,
        path: str,
        default: object,
        **kwargs: object,
    ) -> object:
        """读取非关键补充信息，权限不足或不存在时返回默认值。"""

        try:
            response = await self.client.get(path, **kwargs)
            if response.status_code in {403, 404}:
                return default
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"Provider {self.name} 请求失败：{exc.response.status_code} {path}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"Provider {self.name} 请求失败：{path}：{exc}") from exc

    async def post_json(self, path: str, payload: dict[str, object]) -> object:
        """执行 POST，并将平台错误转换为不泄露凭据的异常。"""

        try:
            response = await self.client.post(path, json=payload)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"Provider {self.name} 请求失败：{exc.response.status_code} {path}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"Provider {self.name} 请求失败：{path}：{exc}") from exc

    async def set_commit_status(
        self,
        repository: RepositoryConfig,
        sha: str,
        *,
        state: Literal["pending", "success", "failure", "error"],
        context: str,
        description: str,
    ) -> None:
        """写入提交状态；具体平台必须显式实现。"""

        raise ProviderError(f"Provider {self.name} 不支持提交状态回写")

    @abstractmethod
    async def list_change_requests(
        self,
        repository: RepositoryConfig,
        *,
        updated_since: datetime | None = None,
    ) -> list[ChangeRequestSnapshot]:
        """列出数量上限内、指定时间之后更新的变更请求。"""

    async def list_change_request_activities(
        self,
        repository: RepositoryConfig,
        number: int,
        *,
        cursor: dict[str, object] | None = None,
        since: datetime | None = None,
    ) -> ChangeRequestActivityBatch | None:
        """读取单个 MR/PR 的增量或限定时间窗口活动；不支持时返回空能力。"""

        return None


def parse_datetime(value: str | None) -> datetime:
    """解析平台 ISO 时间；缺失时使用当前 UTC 时间。"""

    if not value:
        return datetime.now(UTC)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def create_provider(
    name: str,
    config: ProviderConfig,
    scanner: ScannerConfig,
    *,
    token: str | None = None,
) -> BaseProvider:
    """根据配置创建平台适配器。"""

    if config.kind == "github":
        from .github import GitHubProvider

        return GitHubProvider(name, config, scanner, token=token)
    if config.kind == "gitlab":
        from .gitlab import GitLabProvider

        return GitLabProvider(name, config, scanner, token=token)
    raise ProviderError(f"不支持的 Provider 类型：{config.kind}")
