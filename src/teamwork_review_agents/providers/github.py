"""GitHub Pull Request API 适配器。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from ..config import RepositoryConfig
from ..models import ChangeRequestSnapshot
from .base import BaseProvider, ProviderError, parse_datetime


class GitHubProvider(BaseProvider):
    """将 GitHub Pull Request 规范化为统一快照。"""

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "teamwork-review-agents",
        }

    async def list_change_requests(
        self,
        repository: RepositoryConfig,
        *,
        updated_since: datetime | None = None,
    ) -> list[ChangeRequestSnapshot]:
        """自动分页读取最近更新的 PR，并在时间水位处提前停止。"""

        pulls: list[dict[str, Any]] = []
        page = 1
        reached_watermark = False
        while len(pulls) < self.scanner.max_items_per_repository:
            remaining = self.scanner.max_items_per_repository - len(pulls)
            page_size = min(self.scanner.api_page_size, remaining)
            payload = await self.get_json(
                f"repos/{repository.project}/pulls",
                params={
                    "state": "all",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": page_size,
                    "page": page,
                },
            )
            if not isinstance(payload, list):
                raise ProviderError("GitHub Pull Request 列表返回格式异常")
            for item in payload:
                if not isinstance(item, dict):
                    continue
                if updated_since and parse_datetime(item.get("updated_at")) < updated_since:
                    reached_watermark = True
                    break
                pulls.append(item)
                if len(pulls) >= self.scanner.max_items_per_repository:
                    break
            if reached_watermark or len(payload) < page_size:
                break
            page += 1

        semaphore = asyncio.Semaphore(8)

        async def guarded(item: dict[str, Any]) -> ChangeRequestSnapshot:
            async with semaphore:
                return await self._build_snapshot(repository, item)

        return await asyncio.gather(*(guarded(item) for item in pulls))

    async def _build_snapshot(
        self,
        repository: RepositoryConfig,
        item: dict[str, Any],
    ) -> ChangeRequestSnapshot:
        """并行读取单个 PR 的补充信息。"""

        number = int(item["number"])
        detail_result, reviews_result, status_result = await asyncio.gather(
            self.get_json(f"repos/{repository.project}/pulls/{number}"),
            self.get_optional_json(
                f"repos/{repository.project}/pulls/{number}/reviews",
                [],
                params={"per_page": 100},
            ),
            self.get_optional_json(
                f"repos/{repository.project}/commits/{item['head']['sha']}/status",
                {},
            ),
        )
        if not isinstance(detail_result, dict):
            raise ProviderError(f"GitHub PR #{number} 详情返回格式异常")
        detail = detail_result
        reviews = reviews_result if isinstance(reviews_result, list) else []
        status = status_result if isinstance(status_result, dict) else {}

        latest_reviews: dict[str, tuple[str, str]] = {}
        for review in reviews:
            if not isinstance(review, dict) or not isinstance(review.get("user"), dict):
                continue
            login = str(review["user"].get("login", ""))
            submitted_at = str(review.get("submitted_at") or "")
            current = latest_reviews.get(login)
            if login and (current is None or submitted_at >= current[0]):
                latest_reviews[login] = (submitted_at, str(review.get("state", "")))
        approvals = sum(
            1 for _, state in latest_reviews.values() if state.upper() == "APPROVED"
        )

        if detail.get("merged_at"):
            normalized_state = "merged"
        elif detail.get("state") == "closed":
            normalized_state = "closed"
        else:
            normalized_state = "opened"

        mergeable = detail.get("mergeable")
        mergeable_state = str(detail.get("mergeable_state") or "unknown")
        if mergeable is True and mergeable_state == "clean":
            merge_status = "mergeable"
        elif mergeable is False and mergeable_state == "dirty":
            merge_status = "conflict"
        else:
            merge_status = mergeable_state

        head = detail.get("head") or item.get("head") or {}
        base = detail.get("base") or item.get("base") or {}
        return ChangeRequestSnapshot(
            provider=self.name,
            repository_id=repository.id,
            number=number,
            title=str(detail.get("title") or ""),
            state=normalized_state,
            draft=bool(detail.get("draft", False)),
            source_branch=str(head.get("ref") or ""),
            target_branch=str(base.get("ref") or ""),
            head_sha=str(head.get("sha") or ""),
            labels=tuple(
                sorted(
                    str(label.get("name"))
                    for label in detail.get("labels", [])
                    if isinstance(label, dict) and label.get("name") is not None
                )
            ),
            approvals=approvals,
            pipeline_status=str(status.get("state") or "unknown"),
            merge_status=merge_status,
            updated_at=parse_datetime(detail.get("updated_at")),
            web_url=str(detail.get("html_url") or ""),
            raw={"pull_request": detail, "reviews": reviews, "status": status},
        )
