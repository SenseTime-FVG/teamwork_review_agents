"""GitLab Merge Request API 适配器。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ..config import RepositoryConfig
from ..models import ChangeRequestSnapshot
from .base import BaseProvider, ProviderError, parse_datetime


class GitLabProvider(BaseProvider):
    """将 GitLab Merge Request 规范化为统一快照。"""

    def headers(self) -> dict[str, str]:
        return {
            "PRIVATE-TOKEN": self.token,
            "Accept": "application/json",
            "User-Agent": "teamwork-review-agents",
        }

    async def create_change_request_comment(
        self,
        repository: RepositoryConfig,
        number: int,
        body: str,
    ) -> str:
        """通过 GitLab Notes API 创建 MR 顶层评论。"""

        project = quote(repository.project, safe="")
        payload = await self.post_json(
            f"projects/{project}/merge_requests/{number}/notes",
            {"body": body},
        )
        if not isinstance(payload, dict) or payload.get("id") is None:
            raise ProviderError("GitLab MR 评论返回格式异常")
        return str(payload["id"])

    async def update_change_request_comment(
        self,
        repository: RepositoryConfig,
        comment_id: str,
        body: str,
        *,
        number: int | None = None,
    ) -> bool:
        """更新已有 GitLab MR 评论；已被删除时交由调用方重建。"""

        if number is None:
            raise ProviderError("更新 GitLab MR 评论时必须提供变更请求编号")
        project = quote(repository.project, safe="")
        payload = await self.put_optional_json(
            f"projects/{project}/merge_requests/{number}/notes/"
            f"{quote(comment_id, safe='')}",
            {"body": body},
        )
        return payload is not None

    async def delete_change_request_comment(
        self,
        repository: RepositoryConfig,
        comment_id: str,
        *,
        number: int | None = None,
    ) -> None:
        """删除 GitLab MR 评论；评论已不存在时按成功处理。"""

        if number is None:
            raise ProviderError("删除 GitLab MR 评论时必须提供变更请求编号")
        project = quote(repository.project, safe="")
        await self.delete_resource(
            f"projects/{project}/merge_requests/{number}/notes/"
            f"{quote(comment_id, safe='')}",
            missing_ok=True,
        )

    async def get_branch_head(
        self,
        repository: RepositoryConfig,
        branch: str,
    ) -> str:
        """通过 Repository Branch API 读取 GitLab 分支当前提交。"""

        project = quote(repository.project, safe="")
        encoded_branch = quote(branch, safe="")
        payload = await self.get_json(
            f"projects/{project}/repository/branches/{encoded_branch}"
        )
        if not isinstance(payload, dict):
            raise ProviderError(f"GitLab 分支 {branch} 返回格式异常")
        commit = payload.get("commit") or {}
        if not isinstance(commit, dict) or not commit.get("id"):
            raise ProviderError(f"GitLab 分支 {branch} 缺少 Head SHA")
        return str(commit["id"])

    async def list_change_requests(
        self,
        repository: RepositoryConfig,
        *,
        updated_since: datetime | None = None,
    ) -> list[ChangeRequestSnapshot]:
        """自动分页读取最近更新的 MR，并在时间水位处提前停止。"""

        project = quote(repository.project, safe="")
        merge_requests: list[dict[str, Any]] = []
        page = 1
        reached_watermark = False
        while len(merge_requests) < self.scanner.max_items_per_repository:
            remaining = self.scanner.max_items_per_repository - len(merge_requests)
            page_size = min(self.scanner.api_page_size, remaining)
            payload = await self.get_json(
                f"projects/{project}/merge_requests",
                params={
                    "scope": "all",
                    "state": "all",
                    "order_by": "updated_at",
                    "sort": "desc",
                    "per_page": page_size,
                    "page": page,
                },
            )
            if not isinstance(payload, list):
                raise ProviderError("GitLab Merge Request 列表返回格式异常")
            for item in payload:
                if not isinstance(item, dict):
                    continue
                if updated_since and parse_datetime(item.get("updated_at")) < updated_since:
                    reached_watermark = True
                    break
                merge_requests.append(item)
                if len(merge_requests) >= self.scanner.max_items_per_repository:
                    break
            if reached_watermark or len(payload) < page_size:
                break
            page += 1

        semaphore = asyncio.Semaphore(8)

        async def guarded(item: dict[str, Any]) -> ChangeRequestSnapshot:
            async with semaphore:
                return await self._build_snapshot(repository, project, item)

        return await asyncio.gather(*(guarded(item) for item in merge_requests))

    async def _build_snapshot(
        self,
        repository: RepositoryConfig,
        project: str,
        item: dict[str, Any],
    ) -> ChangeRequestSnapshot:
        """并行读取单个 MR 的详情与审批。"""

        iid = int(item["iid"])
        detail_result, approvals_result = await asyncio.gather(
            self.get_json(f"projects/{project}/merge_requests/{iid}"),
            self.get_optional_json(
                f"projects/{project}/merge_requests/{iid}/approvals",
                {},
            ),
        )
        if not isinstance(detail_result, dict):
            raise ProviderError(f"GitLab MR !{iid} 详情返回格式异常")
        detail = detail_result
        approvals = approvals_result if isinstance(approvals_result, dict) else {}

        state = str(detail.get("state") or "opened")
        if state == "merged":
            normalized_state = "merged"
        elif state == "closed":
            normalized_state = "closed"
        else:
            normalized_state = "opened"

        pipeline = detail.get("head_pipeline") or {}
        if not isinstance(pipeline, dict):
            pipeline = {}
        approved_by = approvals.get("approved_by") or []
        diff_refs = detail.get("diff_refs") or {}
        if not isinstance(diff_refs, dict):
            diff_refs = {}
        source_project = ""
        source_project_id = detail.get("source_project_id")
        target_project_id = detail.get("target_project_id")
        if (
            source_project_id is not None
            and target_project_id is not None
            and str(source_project_id) != str(target_project_id)
        ):
            source_project_result = await self.get_optional_json(
                f"projects/{quote(str(source_project_id), safe='')}",
                {},
            )
            if isinstance(source_project_result, dict):
                source_project = str(
                    source_project_result.get("path_with_namespace") or ""
                ).strip()
            if not source_project:
                source_project = str(source_project_id)
        return ChangeRequestSnapshot(
            provider=self.name,
            repository_id=repository.id,
            number=iid,
            title=str(detail.get("title") or ""),
            state=normalized_state,
            draft=bool(detail.get("draft", detail.get("work_in_progress", False))),
            source_branch=str(detail.get("source_branch") or ""),
            target_branch=str(detail.get("target_branch") or ""),
            head_sha=str(detail.get("sha") or diff_refs.get("head_sha") or ""),
            source_project=source_project,
            labels=tuple(sorted(str(label) for label in detail.get("labels", []))),
            approvals=len(approved_by),
            pipeline_status=str(pipeline.get("status") or "unknown"),
            merge_status=str(
                detail.get("detailed_merge_status")
                or detail.get("merge_status")
                or "unknown"
            ),
            created_at=(
                parse_datetime(detail.get("created_at"))
                if detail.get("created_at")
                else None
            ),
            updated_at=parse_datetime(detail.get("updated_at")),
            web_url=str(detail.get("web_url") or ""),
            raw={"merge_request": detail, "approvals": approvals},
        )
