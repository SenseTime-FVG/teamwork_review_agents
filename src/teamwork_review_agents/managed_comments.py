"""按源版本代次维护 GitHub PR 与 GitLab MR 顶层评论。"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from .config import AppConfig
from .environment import resolve_provider_token
from .locks import ResourceLease
from .models import InvocationContext, stable_hash
from .providers.base import create_provider
from .state import StateStore


_COMMENT_LIMIT_BYTES = 60 * 1024


class ManagedCommentService:
    """把远端评论映射、并发保护与平台差异收口到服务侧。"""

    def __init__(self, config: AppConfig, store: StateStore) -> None:
        self.config = config
        self.store = store
        self.repositories = {
            repository.id: repository for repository in config.repositories
        }

    async def publish_agent_comment(
        self,
        context: InvocationContext,
        body: str,
    ) -> dict[str, Any]:
        """使用可信调用上下文发布当前 Agent 的托管顶层评论。"""

        agent = self.config.agents.get(context.current_agent)
        if agent is None:
            raise RuntimeError(f"当前 Agent 不存在：{context.current_agent}")
        if not agent.managed_comment:
            raise PermissionError(
                f"Agent {context.current_agent} 未启用托管顶层评论"
            )
        if "change_request" not in agent.write_scopes:
            raise PermissionError(
                f"Agent {context.current_agent} 缺少 change_request 写作用域"
            )
        if not agent.managed_comment_slot:
            raise RuntimeError(
                f"Agent {context.current_agent} 缺少稳定的托管评论槽位"
            )
        snapshot = context.event.current_snapshot
        return await self.publish(
            repository_id=context.event.repository_id,
            number=context.event.number,
            namespace="agent",
            slot=agent.managed_comment_slot,
            source_generation=context.event.source_generation,
            source_head_sha=snapshot.head_sha,
            body=body,
        )

    async def publish(
        self,
        *,
        repository_id: str,
        number: int,
        namespace: str,
        slot: str,
        source_generation: int,
        source_head_sha: str,
        body: str,
    ) -> dict[str, Any]:
        """创建或更新一个命名槽位在当前源代次的评论。"""

        normalized_body = body.strip()
        if not normalized_body:
            raise ValueError("publish_comment.body 必须是非空字符串")
        if len(normalized_body.encode("utf-8")) > _COMMENT_LIMIT_BYTES:
            raise ValueError("publish_comment.body 不能超过 60 KiB")
        if source_generation < 1:
            raise ValueError("托管评论的源版本代次必须大于零")
        repository = self._repository(repository_id)
        resource_key = self._resource_key(
            repository_id=repository_id,
            number=number,
            namespace=namespace,
            slot=slot,
            source_generation=source_generation,
        )
        async with ResourceLease(
            self.store,
            [resource_key],
            f"managed-comment:{uuid.uuid4()}",
            ttl_seconds=self.config.runtime.lock_ttl_seconds,
            timeout_seconds=self.config.runtime.lock_timeout_seconds,
        ):
            record = await asyncio.to_thread(
                self.store.get_managed_comment,
                repository_id=repository_id,
                number=number,
                namespace=namespace,
                slot=slot,
                source_generation=source_generation,
            )
            content_hash = stable_hash(normalized_body)
            provider_config = self.config.providers[repository.provider]
            token = resolve_provider_token(self.config, provider_config)
            async with create_provider(
                repository.provider,
                provider_config,
                self.config.scanner,
                token=token,
            ) as remote:
                if record is None:
                    remote_comment_id = await remote.create_change_request_comment(
                        repository,
                        number,
                        normalized_body,
                    )
                    action = "created"
                else:
                    remote_comment_id = str(record["remote_comment_id"])
                    updated = await remote.update_change_request_comment(
                        repository,
                        remote_comment_id,
                        normalized_body,
                        number=number,
                    )
                    if updated:
                        action = (
                            "unchanged"
                            if record["content_hash"] == content_hash
                            else "updated"
                        )
                    else:
                        remote_comment_id = (
                            await remote.create_change_request_comment(
                                repository,
                                number,
                                normalized_body,
                            )
                        )
                        action = "recreated"
            await asyncio.to_thread(
                self.store.save_managed_comment,
                repository_id=repository_id,
                number=number,
                namespace=namespace,
                slot=slot,
                source_generation=source_generation,
                remote_comment_id=remote_comment_id,
                source_head_sha=source_head_sha,
                content_hash=content_hash,
            )
        return {
            "action": action,
            "comment_id": remote_comment_id,
            "source_generation": source_generation,
        }

    async def delete(
        self,
        *,
        repository_id: str,
        number: int,
        namespace: str,
        slot: str,
        source_generation: int,
    ) -> bool:
        """删除当前代次的远端评论和本地映射。"""

        repository = self._repository(repository_id)
        resource_key = self._resource_key(
            repository_id=repository_id,
            number=number,
            namespace=namespace,
            slot=slot,
            source_generation=source_generation,
        )
        async with ResourceLease(
            self.store,
            [resource_key],
            f"managed-comment:{uuid.uuid4()}",
            ttl_seconds=self.config.runtime.lock_ttl_seconds,
            timeout_seconds=self.config.runtime.lock_timeout_seconds,
        ):
            record = await asyncio.to_thread(
                self.store.get_managed_comment,
                repository_id=repository_id,
                number=number,
                namespace=namespace,
                slot=slot,
                source_generation=source_generation,
            )
            if record is None:
                return False
            provider_config = self.config.providers[repository.provider]
            token = resolve_provider_token(self.config, provider_config)
            async with create_provider(
                repository.provider,
                provider_config,
                self.config.scanner,
                token=token,
            ) as remote:
                await remote.delete_change_request_comment(
                    repository,
                    str(record["remote_comment_id"]),
                    number=number,
                )
            await asyncio.to_thread(
                self.store.delete_managed_comment,
                repository_id=repository_id,
                number=number,
                namespace=namespace,
                slot=slot,
                source_generation=source_generation,
            )
        return True

    def _repository(self, repository_id: str):
        """按 ID 解析仓库并返回清晰错误。"""

        repository = self.repositories.get(repository_id)
        if repository is None:
            raise RuntimeError(f"托管评论引用了不存在的仓库：{repository_id}")
        return repository

    @staticmethod
    def _resource_key(
        *,
        repository_id: str,
        number: int,
        namespace: str,
        slot: str,
        source_generation: int,
    ) -> str:
        """生成不会泄露槽位文本的评论级写锁键。"""

        return "managed-comment:" + stable_hash(
            repository_id,
            number,
            namespace,
            slot,
            source_generation,
        )
