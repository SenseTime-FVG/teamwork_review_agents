"""GitLab Merge Request API 适配器。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

import httpx

from ..config import RepositoryConfig
from ..models import ChangeRequestActivity, ChangeRequestActivityBatch, ChangeRequestSnapshot
from .base import BaseProvider, ProviderError, parse_datetime


class GitLabProvider(BaseProvider):
    """将 GitLab Merge Request 规范化为统一快照。"""

    supports_activities = True
    ACTIVITY_PAGE_SIZE = 100
    MAX_ACTIVITY_PAGES = 100

    async def _activity_pages(self, path: str) -> list[dict[str, Any]]:
        """完整读取结构化活动；分页不完整时不提交游标，也不读取 Notes。"""

        items: dict[int, dict[str, Any]] = {}
        for page in range(1, self.MAX_ACTIVITY_PAGES + 1):
            payload, headers = await self.get_json_response(
                path, params={"page": page, "per_page": self.ACTIVITY_PAGE_SIZE},
            )
            if not isinstance(payload, list):
                raise ProviderError("GitLab 活动列表返回格式异常")
            for item in payload:
                if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                    raise ProviderError("GitLab 活动缺少稳定 ID")
                items[item["id"]] = item
            next_page = headers.get("x-next-page")
            if next_page == "" or (next_page is None and len(payload) < self.ACTIVITY_PAGE_SIZE):
                return list(items.values())
            if next_page is not None and next_page != str(page + 1):
                raise ProviderError("GitLab 活动分页不连续，暂不推进游标")
        raise ProviderError("GitLab 活动分页超过安全上限，暂不推进游标")

    @staticmethod
    def _activity_time(value: object) -> datetime:
        """活动必须有真实平台时间，不能用当前时间替代缺失值。"""

        if not isinstance(value, str) or not value:
            raise ProviderError("GitLab 活动缺少平台时间")
        try:
            return parse_datetime(value)
        except ValueError as exc:
            raise ProviderError("GitLab 活动时间格式异常") from exc

    async def list_change_request_activities(
        self,
        repository: RepositoryConfig,
        number: int,
        *,
        cursor: dict[str, object] | None = None,
        since: datetime | None = None,
    ) -> ChangeRequestActivityBatch:
        """合并 GitLab 结构化活动，失败时保留游标并交由快照检测兜底。"""

        path = f"projects/{quote(repository.project, safe='')}/merge_requests/{number}"
        previous = dict(cursor or {})
        try:
            responses = await asyncio.gather(
                self.get_json(path),
                self._activity_pages(f"{path}/resource_state_events"),
                self._activity_pages(f"{path}/resource_label_events"),
                self._activity_pages(f"{path}/versions"),
                return_exceptions=True,
            )
            # 等待所有只读请求结束，再处理失败，避免客户端关闭后仍有后台请求运行。
            for response in responses:
                if isinstance(response, BaseException):
                    raise response
            detail, states, labels, versions = responses
            if not isinstance(detail, dict):
                raise ProviderError("GitLab MR 详情返回格式异常")
            return self._activity_batch(number, detail, states, labels, versions, previous, since)
        except (ProviderError, ValueError, TypeError) as exc:
            # 不记录原始异常/响应，避免平台正文或代理信息带入页面；失败不推进成功水位。
            status = exc.__cause__.response.status_code if isinstance(exc.__cause__, httpx.HTTPStatusError) else None
            suffix = f"（HTTP {status}）" if status else ""
            previous["activity_error"] = f"GitLab 结构化活动读取失败{suffix}，暂用快照检测；请检查平台接口与权限，或稍后重试"
            previous["gitlab_rebaseline"] = True
            return ChangeRequestActivityBatch(
                baseline=True, cursor=previous,
                latest_activity=self._cached_activity(previous),
            )

    @staticmethod
    def _cached_activity(cursor: dict[str, Any]) -> ChangeRequestActivity | None:
        """只恢复已验证过的最新活动，兼容旧游标。"""

        value = cursor.get("latest_activity")
        if not isinstance(value, dict):
            return None
        try:
            return ChangeRequestActivity.model_validate(value)
        except ValueError:
            return None

    def _activity_batch(
        self, number: int, detail: dict[str, Any],
        states: list[dict[str, Any]], labels: list[dict[str, Any]],
        versions: list[dict[str, Any]], previous: dict[str, Any],
        since: datetime | None,
    ) -> ChangeRequestActivityBatch:
        """归一化完整活动后按各来源稳定 ID 增量筛选。"""

        records: list[tuple[str, int, ChangeRequestActivity]] = []
        marks = {"state": 0, "label": 0, "version": 0}
        detail_keys: list[str] = []

        def append(source: str, identity: int, kind: str, timestamp: object, **data: Any) -> None:
            """来源前缀隔离 ID；快照已反映的延迟活动不重复生成语义事件。"""

            records.append((source, identity, ChangeRequestActivity(
                id=f"gitlab:{number}:{source}:{identity}", type=kind,
                occurred_at=self._activity_time(timestamp),
                data={**data, "skip_if_unchanged": True},
            )))

        for item in states:
            identity = item["id"]
            marks["state"] = max(marks["state"], identity)
            kind = {"opened": "reopened", "reopened": "reopened", "closed": "closed", "merged": "merged"}.get(item.get("state"))
            if kind:
                append("state", identity, kind, item.get("created_at"))
        for item in labels:
            identity = item["id"]
            marks["label"] = max(marks["label"], identity)
            kind = {"add": "labeled", "remove": "unlabeled"}.get(item.get("action"))
            label = item.get("label")
            if kind and isinstance(label, dict) and label.get("name"):
                append("label", identity, kind, item.get("created_at"), label=str(label["name"]))
        last_head: str | None = None
        for item in sorted(versions, key=lambda value: value["id"]):
            identity = item["id"]
            marks["version"] = max(marks["version"], identity)
            head = item.get("head_commit_sha")
            if not isinstance(head, str) or not head:
                raise ProviderError("GitLab diff version 缺少 Head SHA")
            # 初始版本属于 MR 创建；仅目标基线变化而 Head 不变不算提交更新。
            if last_head is not None and head != last_head:
                append("version", identity, "committed", item.get("created_at"), sha=head)
            last_head = head
        observed_head = detail.get("sha") or (detail.get("diff_refs") or {}).get("head_sha")
        if last_head and observed_head and last_head != observed_head:
            raise ProviderError("GitLab MR 与差异版本尚未同步，暂不推进活动游标")
        for kind, field in (("opened", "created_at"), ("closed", "closed_at"), ("merged", "merged_at")):
            value = detail.get(field)
            if not value:
                continue
            timestamp = self._activity_time(value)
            # 同一关闭/合并优先采用状态流的稳定 ID，不重复合成详情事件。
            if any(activity.type == kind and activity.occurred_at == timestamp for _, _, activity in records):
                continue
            key = f"gitlab:{number}:detail:{kind}:{timestamp.isoformat()}"
            detail_keys.append(key)
            records.append(("detail", 0, ChangeRequestActivity(
                id=key, type=kind, occurred_at=timestamp,
                data={"skip_if_unchanged": True},
            )))
        # 跨来源没有共享序号；同一毫秒先按动作类型、再按同来源数值 ID 稳定排序。
        priority = {"opened": 0, "committed": 1, "labeled": 2, "unlabeled": 2, "reopened": 3, "closed": 3, "merged": 4}
        records.sort(key=lambda row: (
            row[2].occurred_at or datetime.min.replace(tzinfo=UTC),
            priority.get(row[2].type, 0), row[0], row[1], row[2].id,
        ))
        baseline = (not previous and since is None) or bool(previous.get("gitlab_rebaseline")) or (
            bool(previous) and previous.get("gitlab_activity_version") != 1
        )
        old_marks = previous.get("gitlab_watermarks") or {}
        old_keys = previous.get("gitlab_detail_keys") or []
        activities = []
        if not baseline:
            for source, identity, activity in records:
                # 创建动作由首次扫描逻辑处理，但仍可显示、供用户手动重放。
                if activity.type == "opened":
                    continue
                if since is not None and activity.occurred_at < since:
                    continue
                if source == "detail":
                    if activity.id in old_keys:
                        continue
                elif identity <= int(old_marks.get(source, 0)):
                    continue
                activities.append(activity)
        next_cursor = {
            "gitlab_activity_version": 1,
            "gitlab_watermarks": {key: max(value, int(old_marks.get(key, 0))) for key, value in marks.items()},
            "gitlab_detail_keys": sorted(set(old_keys) | set(detail_keys)),
        }
        latest = records[-1][2] if records else None
        if latest is not None:
            next_cursor["latest_activity"] = latest.model_dump(mode="json")
        return ChangeRequestActivityBatch(
            activities=tuple(activities), latest_activity=latest,
            cursor=next_cursor, baseline=baseline,
            observed_head_sha=observed_head,
            observed_state=detail.get("state"),
        )

    def headers(self) -> dict[str, str]:
        return {
            "PRIVATE-TOKEN": self.token,
            "Accept": "application/json",
            "User-Agent": "teamwork-review-agents",
        }

    async def set_commit_status(
        self,
        repository: RepositoryConfig,
        sha: str,
        *,
        state: Literal["pending", "success", "failure", "error"],
        context: str,
        description: str,
        ref: str | None = None,
        source_project: str | None = None,
    ) -> None:
        """在 MR 源提交对应的流水线中写入 GitLab 外部作业状态。"""

        project = quote(source_project or repository.project, safe="")
        payload = {
            "state": {"failure": "failed", "error": "failed"}.get(state, state),
            "name": context,
            "description": description[:255],
        }
        if ref:
            payload["ref"] = ref
        path = f"projects/{project}/statuses/{quote(sha, safe='')}"
        for attempt in range(3):
            try:
                await self.post_json(path, payload)
                return
            except ProviderError as exc:
                # GitLab 同一 SHA/ref 的状态更新可能短暂冲突，仅重试明确的 409。
                cause = exc.__cause__
                if (
                    not isinstance(cause, httpx.HTTPStatusError)
                    or cause.response.status_code != 409
                    or attempt == 2
                ):
                    raise
                await asyncio.sleep(0.2 * (attempt + 1))

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
