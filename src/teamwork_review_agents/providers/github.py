"""GitHub Pull Request API 适配器。"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

from ..config import RepositoryConfig
from ..models import (
    ChangeRequestActivity,
    ChangeRequestActivityBatch,
    ChangeRequestSnapshot,
    stable_hash,
)
from .base import BaseProvider, ProviderError, parse_datetime


class GitHubProvider(BaseProvider):
    """将 GitHub Pull Request 规范化为统一快照。"""

    TIMELINE_EVENT_TYPES = {
        "closed",
        "reopened",
        "merged",
        "committed",
        "head_ref_force_pushed",
        "convert_to_draft",
        "ready_for_review",
        "labeled",
        "unlabeled",
    }
    TIMELINE_PAGE_SIZE = 100
    MAX_TIMELINE_PAGES_PER_SCAN = 100

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

    @staticmethod
    def _timeline_item_id(item: dict[str, Any]) -> str:
        """返回所有 Timeline 项都可使用的稳定标识。"""

        identity = item.get("node_id") or item.get("id")
        if identity is not None:
            return str(identity)
        return stable_hash(
            "github-timeline",
            item.get("event"),
            item.get("sha"),
            item.get("created_at"),
            item.get("url"),
        )

    @staticmethod
    def _last_timeline_page(headers: dict[str, str]) -> int:
        """从 GitHub Link 响应头解析最后一页，缺失时按单页处理。"""

        link = headers.get("link") or headers.get("Link") or ""
        for part in link.split(","):
            if 'rel="last"' not in part:
                continue
            match = re.search(r"[?&]page=(\d+)", part)
            if match:
                return max(1, int(match.group(1)))
        return 1

    @staticmethod
    def _timeline_item_occurred_at(item: dict[str, Any]) -> datetime | None:
        """读取 Timeline 项中平台能够提供的活动时间。"""

        author = item.get("author") or {}
        committer = item.get("committer") or {}
        if not isinstance(author, dict):
            author = {}
        if not isinstance(committer, dict):
            committer = {}
        occurred_value = (
            item.get("created_at")
            or item.get("submitted_at")
            or author.get("date")
            or committer.get("date")
        )
        return parse_datetime(str(occurred_value)) if occurred_value else None

    @classmethod
    def _timeline_activity(
        cls,
        item: dict[str, Any],
    ) -> ChangeRequestActivity | None:
        """将 GitHub Timeline 项收敛为事件检测需要的最小活动。"""

        event_type = str(item.get("event") or "")
        if event_type not in cls.TIMELINE_EVENT_TYPES:
            return None
        label = item.get("label") or {}
        if not isinstance(label, dict):
            label = {}
        return ChangeRequestActivity(
            id=cls._timeline_item_id(item),
            type=event_type,
            occurred_at=cls._timeline_item_occurred_at(item),
            data={
                "sha": str(item.get("sha") or ""),
                "commit_id": str(item.get("commit_id") or ""),
                "label": str(label.get("name") or ""),
            },
        )

    @classmethod
    def _latest_supported_activity(
        cls,
        items: list[object],
    ) -> ChangeRequestActivity | None:
        """按 Timeline 原始顺序返回最后一条可转换活动。"""

        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            activity = cls._timeline_activity(item)
            if activity is not None:
                return activity
        return None

    @staticmethod
    def _cursor_latest_activity(
        cursor: dict[str, object] | None,
    ) -> ChangeRequestActivity | None:
        """兼容读取游标中缓存的最新 Provider 活动。"""

        if not cursor or not isinstance(cursor.get("latest_activity"), dict):
            return None
        try:
            return ChangeRequestActivity.model_validate(cursor["latest_activity"])
        except (TypeError, ValueError):
            return None

    async def list_change_request_activities(
        self,
        repository: RepositoryConfig,
        number: int,
        *,
        cursor: dict[str, object] | None = None,
        since: datetime | None = None,
    ) -> ChangeRequestActivityBatch:
        """按不透明游标增量读取，或首次回看限定时间内的 Timeline。"""

        path = f"repos/{repository.project}/issues/{number}/timeline"
        first_payload, headers = await self.get_json_response(
            path,
            params={"per_page": self.TIMELINE_PAGE_SIZE, "page": 1},
        )
        if not isinstance(first_payload, list):
            raise ProviderError(f"GitHub PR #{number} Timeline 返回格式异常")

        if cursor is None:
            page_cache: dict[int, list[object]] = {1: first_payload}
            last_page = self._last_timeline_page(headers)
            last_payload = first_payload
            if last_page > 1:
                last_payload = await self.get_json(
                    path,
                    params={
                        "per_page": self.TIMELINE_PAGE_SIZE,
                        "page": last_page,
                    },
                )
                if not isinstance(last_payload, list):
                    raise ProviderError(f"GitHub PR #{number} Timeline 返回格式异常")
                page_cache[last_page] = last_payload
            elif len(first_payload) == self.TIMELINE_PAGE_SIZE:
                # 非 GitHub 兼容服务可能省略 Link，首次基线需顺序找到真正末页。
                page = 2
                while True:
                    payload = await self.get_json(
                        path,
                        params={
                            "per_page": self.TIMELINE_PAGE_SIZE,
                            "page": page,
                        },
                    )
                    if not isinstance(payload, list):
                        raise ProviderError(
                            f"GitHub PR #{number} Timeline 返回格式异常"
                        )
                    if payload:
                        last_page = page
                        last_payload = payload
                        page_cache[page] = payload
                    if len(payload) < self.TIMELINE_PAGE_SIZE:
                        break
                    if page >= self.MAX_TIMELINE_PAGES_PER_SCAN:
                        raise ProviderError(
                            f"GitHub PR #{number} Timeline 首次基线超过 "
                            f"{self.MAX_TIMELINE_PAGES_PER_SCAN} 页"
                        )
                    page += 1
            latest_activity: ChangeRequestActivity | None = None
            latest_activity_page = last_page
            latest_activity_pages_read = 0
            while latest_activity_page >= 1:
                payload = page_cache.get(latest_activity_page)
                if payload is None:
                    payload = await self.get_json(
                        path,
                        params={
                            "per_page": self.TIMELINE_PAGE_SIZE,
                            "page": latest_activity_page,
                        },
                    )
                    if not isinstance(payload, list):
                        raise ProviderError(
                            f"GitHub PR #{number} Timeline 返回格式异常"
                        )
                    page_cache[latest_activity_page] = payload
                latest_activity_pages_read += 1
                latest_activity = self._latest_supported_activity(payload)
                if latest_activity is not None:
                    break
                if (
                    latest_activity_pages_read
                    >= self.MAX_TIMELINE_PAGES_PER_SCAN
                    and latest_activity_page > 1
                ):
                    raise ProviderError(
                        f"GitHub PR #{number} Timeline 最新事件回看超过 "
                        f"{self.MAX_TIMELINE_PAGES_PER_SCAN} 页"
                    )
                latest_activity_page -= 1
            latest_id = (
                self._timeline_item_id(last_payload[-1])
                if last_payload and isinstance(last_payload[-1], dict)
                else ""
            )
            next_cursor = {"page": last_page, "item_id": latest_id}
            if since is None:
                return ChangeRequestActivityBatch(
                    cursor=next_cursor,
                    latest_activity=latest_activity,
                    baseline=True,
                )

            # 首次扫描只从末页向前回看到本扫描周期，
            # 避免重放全部历史动作。
            recent_pages: list[tuple[int, list[object]]] = []
            page = last_page
            pages_read = 0
            while page >= 1:
                payload = page_cache.get(page)
                if payload is None:
                    payload = await self.get_json(
                        path,
                        params={
                            "per_page": self.TIMELINE_PAGE_SIZE,
                            "page": page,
                        },
                    )
                if not isinstance(payload, list):
                    raise ProviderError(f"GitHub PR #{number} Timeline 返回格式异常")
                recent_pages.append((page, payload))
                pages_read += 1
                occurred_times = [
                    occurred_at
                    for item in payload
                    if isinstance(item, dict)
                    and (occurred_at := self._timeline_item_occurred_at(item)) is not None
                ]
                if occurred_times and all(value < since for value in occurred_times):
                    break
                if page == 1:
                    break
                if pages_read >= self.MAX_TIMELINE_PAGES_PER_SCAN:
                    raise ProviderError(
                        f"GitHub PR #{number} Timeline 首次回看超过 "
                        f"{self.MAX_TIMELINE_PAGES_PER_SCAN} 页"
                    )
                page -= 1

            entries = [
                item
                for _, payload in reversed(recent_pages)
                for item in payload
                if isinstance(item, dict)
            ]
            activities = tuple(
                activity
                for item in entries
                if (activity := self._timeline_activity(item)) is not None
                and activity.occurred_at is not None
                and activity.occurred_at >= since
            )
            return ChangeRequestActivityBatch(
                activities=activities,
                latest_activity=latest_activity,
                cursor=next_cursor,
            )

        previous_latest_activity = self._cursor_latest_activity(cursor)
        try:
            cursor_page = max(1, int(cursor.get("page") or 1))
        except (TypeError, ValueError):
            cursor_page = 1
        cursor_item_id = str(cursor.get("item_id") or "")
        start_page = max(1, cursor_page - 1)
        entries: list[tuple[int, dict[str, Any]]] = []
        page = start_page
        pages_read = 0
        while True:
            if page == 1:
                payload = first_payload
            else:
                payload = await self.get_json(
                    path,
                    params={"per_page": self.TIMELINE_PAGE_SIZE, "page": page},
                )
            if not isinstance(payload, list):
                raise ProviderError(f"GitHub PR #{number} Timeline 返回格式异常")
            entries.extend(
                (page, item) for item in payload if isinstance(item, dict)
            )
            pages_read += 1
            if len(payload) < self.TIMELINE_PAGE_SIZE:
                break
            if pages_read >= self.MAX_TIMELINE_PAGES_PER_SCAN:
                raise ProviderError(
                    f"GitHub PR #{number} Timeline 单轮增量超过 "
                    f"{self.MAX_TIMELINE_PAGES_PER_SCAN} 页"
                )
            page += 1

        marker_index = next(
            (
                index
                for index, (_, item) in enumerate(entries)
                if self._timeline_item_id(item) == cursor_item_id
            ),
            None,
        )
        earlier_page = start_page - 1
        while marker_index is None and cursor_item_id and earlier_page >= 1:
            payload = await self.get_json(
                path,
                params={
                    "per_page": self.TIMELINE_PAGE_SIZE,
                    "page": earlier_page,
                },
            )
            if not isinstance(payload, list):
                raise ProviderError(f"GitHub PR #{number} Timeline 返回格式异常")
            prefix = [
                (earlier_page, item) for item in payload if isinstance(item, dict)
            ]
            entries = prefix + entries
            pages_read += 1
            marker_index = next(
                (
                    index
                    for index, (_, item) in enumerate(entries)
                    if self._timeline_item_id(item) == cursor_item_id
                ),
                None,
            )
            if pages_read >= self.MAX_TIMELINE_PAGES_PER_SCAN:
                break
            earlier_page -= 1

        if not entries:
            return ChangeRequestActivityBatch(
                cursor={"page": 1, "item_id": ""},
                latest_activity=previous_latest_activity,
                baseline=bool(cursor_item_id),
            )

        latest_page, latest_item = entries[-1]
        next_cursor = {
            "page": latest_page,
            "item_id": self._timeline_item_id(latest_item),
        }
        if cursor_item_id and marker_index is None:
            # 游标对应的 Timeline 项可能被删除；重建基线比重放历史更安全。
            latest_activity = self._latest_supported_activity(
                [item for _, item in entries]
            )
            return ChangeRequestActivityBatch(
                cursor=next_cursor,
                latest_activity=latest_activity or previous_latest_activity,
                baseline=True,
            )

        new_entries = entries[(marker_index + 1) if marker_index is not None else 0 :]
        activities = tuple(
            activity
            for _, item in new_entries
            if (activity := self._timeline_activity(item)) is not None
        )
        latest_activity = activities[-1] if activities else previous_latest_activity
        return ChangeRequestActivityBatch(
            activities=activities,
            latest_activity=latest_activity,
            cursor=next_cursor,
        )

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
            created_at=(
                parse_datetime(detail.get("created_at"))
                if detail.get("created_at")
                else None
            ),
            updated_at=parse_datetime(detail.get("updated_at")),
            web_url=str(detail.get("html_url") or ""),
            raw={"pull_request": detail, "reviews": reviews, "status": status},
        )
