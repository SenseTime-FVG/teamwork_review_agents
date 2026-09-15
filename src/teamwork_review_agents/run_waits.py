"""跨内嵌模型、CLI 和 MCP 进程共享的受控等待记录。"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import StateStore


CI_TIMEOUT_MESSAGE = "等待远端 CI 超时，待处理；已保留工作区、分支和 PR，不自动重跑或合并"


class RunWaits:
    """仅保存调度状态；同一运行、PR 和源提交重复等待不重置期限。"""

    def __init__(self, store: StateStore):
        self.store = store

    def begin(self, run_id: str, kind: str, key: str, timeout: float) -> dict:
        """只为仍在执行的运行登记等待，旧记录保留最初截止时间。"""

        now = time.time()
        with self.store.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM agent_runs WHERE run_id=? AND status='running' AND cancel_requested=0", (run_id,),
            ).fetchone() is None:
                raise RuntimeError("运行已停止，不能开始新的等待")
            connection.execute(
                "INSERT OR IGNORE INTO agent_run_waits(run_id,kind,wait_key,started_at,deadline) "
                "SELECT run_id,?,?,?,? FROM agent_runs "
                "WHERE run_id=? AND status='running' AND cancel_requested=0",
                (kind, key, now, now + timeout, run_id),
            )
            connection.execute(
                "UPDATE agent_run_waits SET status='waiting' WHERE run_id=? AND kind=? AND wait_key=? "
                "AND status='finished' AND EXISTS(SELECT 1 FROM agent_runs WHERE run_id=? AND status='running' AND cancel_requested=0)",
                (run_id, kind, key, run_id),
            )
            row = connection.execute(
                "SELECT * FROM agent_run_waits WHERE run_id=? AND kind=? AND wait_key=?",
                (run_id, kind, key),
            ).fetchone()
        if row is None:
            raise RuntimeError("运行已停止，不能开始新的等待")
        return dict(row)

    def update(self, run_id: str, kind: str, key: str, status: str, detail: dict | None = None) -> None:
        """终止后的等待不能被迟到的成功结果覆盖。"""

        with self.store.connect() as connection:
            connection.execute(
                "UPDATE agent_run_waits SET status=?,detail=COALESCE(?,detail) "
                "WHERE run_id=? AND kind=? AND wait_key=? AND status='waiting'",
                (status, json.dumps(detail, ensure_ascii=False) if detail is not None else None, run_id, kind, key),
            )

    def list(self, run_id: str) -> list[dict]:
        """返回 UI 与运行看门狗使用的轻量状态。"""

        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_run_waits WHERE run_id=? ORDER BY started_at", (run_id,),
            ).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def state(self, run_id: str) -> tuple[bool, bool]:
        """过期 CI 是不可逆停止信号；过期子调用不再暂停 idle。"""

        now = time.time()
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE agent_run_waits SET status='timed_out' "
                "WHERE run_id=? AND kind='ci' AND status='waiting' AND deadline<=?",
                (run_id, now),
            )
        rows = self.list(run_id)
        with self.store.connect() as connection:
            child_timeout = connection.execute(
                "SELECT 1 FROM agent_run_waits w JOIN agent_runs r ON r.run_id=w.run_id "
                "WHERE r.root_run_id=? AND w.kind='ci' AND w.status='timed_out' LIMIT 1", (run_id,),
            ).fetchone() is not None
        return (
            any(row["status"] == "waiting" and row["deadline"] > now for row in rows),
            child_timeout or any(row["kind"] == "ci" and row["status"] == "timed_out" for row in rows),
        )

    @asynccontextmanager
    async def child(self, run_id: str, key: str, timeout: float):
        """完整 CLI 的子调用由服务登记，不能由模型日志伪造等待。"""

        await asyncio.to_thread(self.begin, run_id, "child", key, timeout)
        try:
            yield
        finally:
            await asyncio.to_thread(self.update, run_id, "child", key, "finished")


def mcp_wait_timeout(config) -> float:
    """为受控 CI 留出平台请求取消和工具响应收尾时间。"""

    limits = [config.runtime.remote_ci_wait_timeout_seconds]
    limits.extend(repo.remote_ci_wait_timeout_seconds or limits[0] for repo in config.repositories)
    # 子任务仍有自己的总时限，但传输需要容纳排队和工作区准备，不能抢先切断委托。
    child_budget = max((agent.timeout_seconds for agent in config.agents.values()), default=1200)
    child_budget += config.runtime.lock_timeout_seconds + config.runtime.repository_initialization_timeout_seconds
    return float(max(config.runtime.mcp_tool_timeout_seconds, max(limits) + 60, child_budget + 60))
