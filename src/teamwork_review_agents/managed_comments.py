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
_UNKNOWN_MODEL_SIGNATURE = "Codex 账号默认（未记录具体模型）"


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
        if agent.managed_comment_model_signature:
            body = await self._append_model_signature(context.run_id, body)
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

    async def _append_model_signature(self, run_id: str, body: str) -> str:
        """使用本轮固化的模型快照为非空评论正文追加签名。"""

        if not body.strip():
            return body
        run = await asyncio.to_thread(self.store.get_run, run_id)
        model_snapshot = run.get("model_snapshot") if run else None
        signature = self._format_model_signature(model_snapshot)
        return f"{body.rstrip()}\n\n---\n_模型：`{signature}`_"

    @staticmethod
    def _format_model_signature(model_snapshot: Any) -> str:
        """把可信模型快照规范为不会破坏 Markdown 的单行签名。"""

        snapshot = model_snapshot if isinstance(model_snapshot, dict) else {}
        model = ManagedCommentService._signature_part(snapshot.get("model"))
        reasoning_effort = ManagedCommentService._signature_part(
            snapshot.get("reasoning_effort")
        )
        if not model:
            return _UNKNOWN_MODEL_SIGNATURE
        if reasoning_effort:
            return f"{model} ({reasoning_effort})"
        return model

    @staticmethod
    def _signature_part(value: Any) -> str:
        """压缩空白并替换反引号，避免配置文本逃逸 Markdown 代码片段。"""

        if value is None:
            return ""
        normalized = " ".join(str(value).split())[:256]
        return normalized.replace("`", "ˋ")

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

    async def publish_latest(
        self,
        *,
        repository_id: str,
        number: int,
        namespace: str,
        slot: str,
        source_generation: int,
        source_head_sha: str,
        body: str,
        replace_existing: bool,
    ) -> dict[str, Any]:
        """在槽位内发布最新评论，可选择先删除全部历史评论。"""

        normalized_body = body.strip()
        if not normalized_body:
            raise ValueError("publish_comment.body 必须是非空字符串")
        if len(normalized_body.encode("utf-8")) > _COMMENT_LIMIT_BYTES:
            raise ValueError("publish_comment.body 不能超过 60 KiB")
        if source_generation < 1:
            raise ValueError("托管评论的源版本代次必须大于零")
        repository = self._repository(repository_id)
        resource_key = self._slot_resource_key(
            repository_id=repository_id,
            number=number,
            namespace=namespace,
            slot=slot,
        )
        async with ResourceLease(
            self.store,
            [resource_key],
            f"managed-comment:{uuid.uuid4()}",
            ttl_seconds=self.config.runtime.lock_ttl_seconds,
            timeout_seconds=self.config.runtime.lock_timeout_seconds,
        ):
            records = await asyncio.to_thread(
                self.store.list_managed_comments,
                repository_id=repository_id,
                number=number,
                namespace=namespace,
                slot=slot,
            )
            if records and not replace_existing:
                current = records[-1]
                return {
                    "action": "preserved",
                    "comment_id": str(current["remote_comment_id"]),
                    "source_generation": int(current["source_generation"]),
                }

            provider_config = self.config.providers[repository.provider]
            token = resolve_provider_token(self.config, provider_config)
            async with create_provider(
                repository.provider,
                provider_config,
                self.config.scanner,
                token=token,
            ) as remote:
                for record in records:
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
                        source_generation=int(record["source_generation"]),
                    )
                remote_comment_id = await remote.create_change_request_comment(
                    repository,
                    number,
                    normalized_body,
                )
            await asyncio.to_thread(
                self.store.save_managed_comment,
                repository_id=repository_id,
                number=number,
                namespace=namespace,
                slot=slot,
                source_generation=source_generation,
                remote_comment_id=remote_comment_id,
                source_head_sha=source_head_sha,
                content_hash=stable_hash(normalized_body),
            )
        return {
            "action": "replaced" if records else "created",
            "comment_id": remote_comment_id,
            "source_generation": source_generation,
        }

    async def delete_all(
        self,
        *,
        repository_id: str,
        number: int,
        namespace: str,
        slot: str,
    ) -> int:
        """删除一个命名槽位下全部源代次的远端评论和本地映射。"""

        repository = self._repository(repository_id)
        resource_key = self._slot_resource_key(
            repository_id=repository_id,
            number=number,
            namespace=namespace,
            slot=slot,
        )
        async with ResourceLease(
            self.store,
            [resource_key],
            f"managed-comment:{uuid.uuid4()}",
            ttl_seconds=self.config.runtime.lock_ttl_seconds,
            timeout_seconds=self.config.runtime.lock_timeout_seconds,
        ):
            records = await asyncio.to_thread(
                self.store.list_managed_comments,
                repository_id=repository_id,
                number=number,
                namespace=namespace,
                slot=slot,
            )
            if not records:
                return 0
            provider_config = self.config.providers[repository.provider]
            token = resolve_provider_token(self.config, provider_config)
            async with create_provider(
                repository.provider,
                provider_config,
                self.config.scanner,
                token=token,
            ) as remote:
                for record in records:
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
                        source_generation=int(record["source_generation"]),
                    )
        return len(records)

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

    @staticmethod
    def _slot_resource_key(
        *,
        repository_id: str,
        number: int,
        namespace: str,
        slot: str,
    ) -> str:
        """生成不会泄露槽位文本的跨代次评论写锁键。"""

        return "managed-comment-slot:" + stable_hash(
            repository_id,
            number,
            namespace,
            slot,
        )
