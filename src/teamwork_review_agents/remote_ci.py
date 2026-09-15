"""在可信仓库范围内只读等待远端 CI，不执行合并或修改平台状态。"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import quote

from .environment import resolve_provider_token
from .providers import create_provider
from .run_waits import CI_TIMEOUT_MESSAGE, RunWaits


CI_TOOL_DESCRIPTION = (
    "等待当前仓库指定 PR/MR 的远端 CI，必须提供期望源提交完整 SHA。"
    "使用此工具代替 shell 长轮询；超时由后台停止运行并保留 PR，不自动重跑。"
    "success 只代表观察到的 CI 通过，不代表分支保护、审批或合并授权通过。"
)
CI_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "number": {"type": "integer", "minimum": 1, "description": "当前仓库 PR/MR 编号。"},
        "expected_head_sha": {"type": "string", "description": "待合并源提交的完整 SHA。"},
    },
    "required": ["number", "expected_head_sha"],
    "additionalProperties": False,
}
CI_RUNTIME_INSTRUCTIONS = (
    "远端 CI 等待必须使用 wait_for_ci(number, expected_head_sha)，不要用 shell sleep 长轮询。"
    "该工具不授予合并权限；返回 success 后仍须核验最新源/目标 SHA、审批与全部平台门禁。"
    "CI 超时或运行超时是待处理终态：保留本次 PR、分支、提交与工作区，不执行失败清理或从头重跑。"
    "此运行时超时保留规则优先于角色 Prompt 的通用失败清理段落。"
)


def validate_ci_arguments(number: Any, expected_head_sha: Any) -> None:
    """参数不能被用作路径、任意 URL 或额外命令。"""

    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise ValueError("PR/MR 编号必须是正整数")
    if not isinstance(expected_head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", expected_head_sha):
        raise ValueError("expected_head_sha 必须是完整提交 SHA")


async def _pages(provider, path: str, field: str) -> list[dict]:
    """仅递增已知端点的页码，不跟随平台返回的任意链接。"""

    result = []
    for page in range(1, 101):
        payload = await provider.get_json(path, params={"per_page": 100, "page": page, "filter": "latest"})
        if not isinstance(payload, dict) or not isinstance(payload.get(field), list):
            raise ValueError("平台 CI 分页响应格式无效")
        items = payload[field]
        if not all(isinstance(item, dict) for item in items):
            raise ValueError("平台 CI 检查项格式无效")
        result.extend(items)
        if len(items) < 100:
            if isinstance(payload.get("total_count"), int) and len(result) < payload["total_count"]:
                raise ValueError("平台 CI 分页未完整返回，不能确认全部通过")
            return result
    raise ValueError("平台 CI 检查项超过分页保护上限，不能确认全部通过")


async def ci_snapshot(provider, repository, number: int, sha: str) -> dict:
    """只返回数字和枚举，平台正文、检查名称及 URL 不进入调度记录。"""

    project = quote(repository.project, safe="/" if provider.config.kind == "github" else "")
    if provider.config.kind == "github":
        root = f"repos/{project}"
        pr = await provider.get_json(f"{root}/pulls/{number}")
        if not isinstance(pr, dict) or not isinstance(pr.get("head"), dict):
            raise ValueError("平台 PR 响应格式无效")
        if str(pr["head"].get("sha", "")).lower() != sha:
            return {"status": "head_changed"}
        if pr.get("state") != "open":
            return {"status": "closed"}
        checks = await _pages(provider, f"{root}/commits/{sha}/check-runs", "check_runs")
        # combined status 的 state 已汇总所有 context；无需逐页拼接旧状态历史。
        statuses = await provider.get_json(f"{root}/commits/{sha}/status")
        if not isinstance(statuses, dict) or not isinstance(statuses.get("total_count"), int):
            raise ValueError("平台 Commit Status 响应格式无效")
        states = []
        for check in checks:
            conclusion = check.get("conclusion")
            if str(check.get("head_sha", "")).lower() != sha or check.get("status") != "completed":
                states.append("pending")
            elif conclusion in {"success", "neutral", "skipped"}:
                states.append("success")
            elif conclusion in {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}:
                states.append("failure")
            else:
                states.append("pending")
        if statuses["total_count"] > 0:
            states.append(str(statuses.get("state", "unknown")) if str(statuses.get("sha", "")).lower() == sha else "pending")
        status = ("failure" if any(state in {"failure", "error"} for state in states)
                  else "success" if states and all(state == "success" for state in states)
                  else "pending")
        return {"status": status, "check_count": len(checks), "status_count": statuses["total_count"]}
    if provider.config.kind == "gitlab":
        mr = await provider.get_json(f"projects/{project}/merge_requests/{number}")
        if not isinstance(mr, dict):
            raise ValueError("平台 MR 响应格式无效")
        if str(mr.get("sha", "")).lower() != sha:
            return {"status": "head_changed"}
        if mr.get("state") != "opened":
            return {"status": "closed"}
        pipeline = mr.get("head_pipeline")
        # 无 pipeline 不是通过；流水线尚未生成、手动等待和未知状态均继续计时。
        if not isinstance(pipeline, dict):
            return {"status": "pending", "pipeline_count": 0}
        state = pipeline.get("status")
        # 源提交发生变化而 head_pipeline 尚未刷新时，不能复用旧流水线的成功结果。
        matching_sha = str(pipeline.get("sha", "")).lower() == sha
        status = ("success" if matching_sha and state == "success" else "failure" if matching_sha and state in {"failed", "canceled"} else "pending")
        return {"status": status, "pipeline_count": 1}
    raise ValueError("当前 Provider 不支持远端 CI 等待")


class RemoteCIWaiter:
    """固定期限的只读轮询，空检查或暂时查询失败不能被误认为成功。"""

    def __init__(self, config, store, *, poll_seconds: float = 15):
        self.config = config
        self.store = store
        self.waits = RunWaits(store)
        self.poll_seconds = poll_seconds

    async def wait(self, context, number: int, expected_head_sha: str, *, cancel_check=None) -> dict:
        """仓库和 Token 只能从服务签发的调用上下文解析。"""

        validate_ci_arguments(number, expected_head_sha)
        sha = expected_head_sha.lower()
        repository_id = context.event.repository_id if context.event else context.schedule.repository_id
        repository = next(repo for repo in self.config.repositories if repo.id == repository_id)
        config = self.config.providers[repository.provider]
        token = resolve_provider_token(self.config, config, repository)
        timeout = repository.remote_ci_wait_timeout_seconds or self.config.runtime.remote_ci_wait_timeout_seconds
        key = f"{number}:{sha}"
        record = await asyncio.to_thread(self.waits.begin, context.run_id, "ci", key, timeout)
        # 持久化墙钟用于跨进程核验；单次等待同时使用单调钟，避免系统校时延长期限。
        deadline = time.monotonic() + max(0, record["deadline"] - time.time())
        outcome = "finished"
        try:
            provider = create_provider(repository.provider, config, self.config.scanner, token=token)
            async with provider:
                while True:
                    if cancel_check is not None and await cancel_check():
                        raise asyncio.CancelledError
                    remaining = min(deadline - time.monotonic(), record["deadline"] - time.time())
                    if remaining <= 0 or record["status"] == "timed_out":
                        outcome = "timed_out"
                        await asyncio.to_thread(self.waits.update, context.run_id, "ci", key, outcome)
                        await asyncio.to_thread(self.store.append_run_log, context.run_id, stream="system",
                                                event_type="ci.wait.timed_out", payload={"number": number, "error": CI_TIMEOUT_MESSAGE})
                        return {"status": "timed_out", "number": number, "error": CI_TIMEOUT_MESSAGE}
                    try:
                        result = await asyncio.wait_for(ci_snapshot(provider, repository, number, sha), remaining)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # 不保存 HTTP 异常原文，避免响应正文或凭据进入模型与 UI。
                        result = {"status": "pending", "query_error": True}
                    if time.monotonic() >= deadline or time.time() >= record["deadline"]:
                        continue
                    await asyncio.to_thread(self.waits.update, context.run_id, "ci", key, "waiting", result)
                    await asyncio.to_thread(self.store.append_run_log, context.run_id, stream="system",
                                            event_type="ci.wait.progress", payload={"number": number, "deadline": record["deadline"], **result})
                    if time.monotonic() >= deadline or time.time() >= record["deadline"]:
                        continue
                    if result["status"] != "pending":
                        return {"number": number, "expected_head_sha": sha, **result}
                    await asyncio.sleep(max(0, min(self.poll_seconds, deadline - time.monotonic())))
        finally:
            await asyncio.to_thread(self.waits.update, context.run_id, "ci", key, outcome)
