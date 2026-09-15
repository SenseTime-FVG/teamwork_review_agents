"""内嵌运行的等待状态与单向停止原因，不向父模型传递子任务进展。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class RunStop:
    """一次确定的终止原因，不能被后续工具返回覆盖。"""

    status: Literal["timed_out", "cancelled"]
    error_code: str
    error: str
    event_type: str = "run.cancelled"


def cancellation_stop(source: str | None) -> RunStop:
    """只有持久化来源明确为管理员时才使用管理员取消文案。"""

    if source == "administrator":
        return RunStop("cancelled", "administrator_cancelled", "运行已由管理员取消")
    if source == "service_shutdown":
        return RunStop("cancelled", "service_shutdown", "服务停止时中断运行")
    return RunStop("cancelled", "run_interrupted", "运行被上层任务或调度器中断，未记录管理员取消请求")


@dataclass
class RunControl:
    """每个异步运行独立计时，仅沿调用链共享已确定的停止原因。"""

    run_id: str
    parent: RunControl | None = None
    last_progress_at: float = field(default_factory=time.monotonic)
    waiting_children: int = 0
    waiting_ci: int = 0
    stop: RunStop | None = None

    def progress(self) -> None:
        """只更新当前运行的进展，不传播到父运行。"""

        self.last_progress_at = time.monotonic()

    @contextmanager
    def waiting_for_child(self) -> Iterator[None]:
        """等待期间暂停自身 idle 判断，离开时重新开始计时。"""

        self.raise_if_stopped()
        self.waiting_children += 1
        try:
            yield
        finally:
            self.waiting_children -= 1
            self.progress()

    def effective_stop(self) -> RunStop | None:
        """父级中断必须穿过子任务的错误处理，且保留真实来源。"""

        if self.stop is not None:
            return self.stop
        parent = self.parent
        while parent is not None:
            if parent.stop is not None:
                self.stop = child_stop(parent.stop, parent.run_id)
                return self.stop
            parent = parent.parent
        return None

    def raise_if_stopped(self) -> None:
        """防止依赖调用吞掉 CancelledError 后继续产生副作用。"""

        # 调度器直接取消协程时可能没有经过本运行看门狗，同样不能被工具吞掉。
        task = asyncio.current_task()
        if self.effective_stop() is None and task is not None and task.cancelling():
            self.stop = cancellation_stop(None)
        if self.effective_stop() is not None:
            raise asyncio.CancelledError


def child_stop(stop: RunStop, parent_run_id: str) -> RunStop:
    """子任务被上层终止不等于子任务自身超时。"""

    if stop.error_code in {"administrator_cancelled", "service_shutdown"}:
        return stop
    return RunStop(
        "cancelled",
        (
            "parent_run_timeout"
            if stop.status == "timed_out" or stop.error_code == "parent_run_timeout"
            else "parent_run_interrupted"
        ),
        f"父任务 {parent_run_id} 已中断，子任务随之停止：{stop.error}",
    )


active_run_control: ContextVar[RunControl | None] = ContextVar("active_run_control", default=None)


def inherited_stop(run_id: str) -> RunStop:
    """为模型启动前的排队、工作区准备中断查找上层原因。"""

    control = active_run_control.get()
    if control is not None:
        stop = control.effective_stop()
        if stop is not None:
            return stop if control.run_id == run_id else child_stop(stop, control.run_id)
    return cancellation_stop(None)
