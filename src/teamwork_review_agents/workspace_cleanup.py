"""按主机本地时间回收过期受管工作区，不触碰基础仓库和依赖缓存。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Callable
import uuid

from .config import AppConfig
from .filesystem import _is_junction
from .locks import LockCancelledError, LockTimeoutError, ResourceLease
from .models import stable_hash
from .scheduler import next_workspace_cleanup_at
from .state import StateStore
from .workspace import WorkspaceError, remove_expired_run_workspace, workspace_usage_lock_key


STATE_KEY = "workspace_cleanup"
MAX_DETAILS = 200
LOGGER = logging.getLogger(__name__)


class CleanupSkipped(ValueError):
    """缺少安全证据或仍在使用时跳过，不把保护动作当成删除失败。"""


def _plain_directory(path: Path) -> None:
    """目录入口不可是符号链接或 Windows 联接，禁止沿父目录越界。"""

    info = path.lstat()
    if path.is_symlink() or _is_junction(info) or not path.is_dir():
        raise CleanupSkipped("受管路径包含目录链接或不是目录，已跳过")


def _same_path(left: str | Path, right: str | Path) -> bool:
    """兼容 Windows 大小写差异，按规范路径比较归属。"""

    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def cleanup_markers(root: Path, problems: list[dict] | None = None) -> list[Path]:
    """只枚举约定层级的外部保留标记，不递归搜索任意目录。"""

    if not root.exists() and not root.is_symlink():
        return []
    _plain_directory(root)
    markers: list[Path] = []
    for bucket in sorted(root.iterdir()):
        if re.fullmatch(r"[0-9a-f]{16}", bucket.name) is None:
            continue
        try:
            _plain_directory(bucket)
            # 不用会忽略部分扫描错误的 glob；无法读取分桶时必须留下诊断。
            entries = sorted(path for path in bucket.iterdir() if path.name.startswith(".") and path.name.endswith(".retained.json"))
        except (OSError, CleanupSkipped) as exc:
            if problems is not None:
                problems.append({"path": str(bucket), "status": "skipped" if isinstance(exc, CleanupSkipped) else "failed", "reason":
                                 str(exc) if isinstance(exc, CleanupSkipped) else "仓库目录无法读取，检查失败"})
            continue
        markers.extend(entries)
    return markers


def _inspect_marker(root: Path, marker: Path, store: StateStore) -> tuple[Path, dict, dict]:
    """目录、标记和数据库记录必须三方吻合，旧记录缺少仓库 ID 时仍需精确路径。"""

    _plain_directory(root)
    _plain_directory(marker.parent)
    info = marker.lstat()
    if marker.parent.parent != root or not stat.S_ISREG(info.st_mode) or _is_junction(info) or info.st_size > 65536:
        raise CleanupSkipped("保留标记路径或格式不可信，已跳过")
    run_id = marker.name.removeprefix(".").removesuffix(".retained.json")
    try:
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError
        payload = json.loads(marker.read_text(encoding="utf-8"))
        target = marker.parent / run_id
        recorded_path = payload["workspace"]
        if not isinstance(recorded_path, str) or not Path(recorded_path).is_absolute():
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise CleanupSkipped("保留标记内容无效，已跳过") from None
    if not _same_path(recorded_path, target):
        raise CleanupSkipped("保留标记与工作区路径不匹配，已跳过")
    if target.exists() or target.is_symlink():
        _plain_directory(target)
        if not _same_path(target.parent, root / marker.parent.name):
            raise CleanupSkipped("工作区越出受管目录，已跳过")
    record = store.workspace_cleanup_record(run_id)
    if not record or not record["workspace_path"] or not _same_path(record["workspace_path"], target):
        raise CleanupSkipped("数据库无法确认此工作区归属，已跳过")
    if record["repository_id"] and stable_hash(record["repository_id"])[:16] != marker.parent.name:
        raise CleanupSkipped("工作区与数据库仓库身份不匹配，已跳过")
    if record["workspace_status"] == "removed" and target.exists():
        raise CleanupSkipped("记录已清理但目录重新出现，无法确认新内容归属，已跳过")
    return target, payload, record


def _check_inactive(store: StateStore, target: Path, record: dict) -> None:
    """保护整棵活动任务树，以及其他任务引用的同一目录或其子目录。"""

    for user in store.active_workspace_users():
        if user["run_id"] == record["run_id"] or user["root_run_id"] == record["root_run_id"]:
            raise CleanupSkipped("任务或其父子 Agent 仍在排队、准备或运行，已跳过")
        if user["workspace_path"]:
            active = Path(user["workspace_path"]).resolve()
            if _same_path(active, target) or target in active.parents or active in target.parents:
                raise CleanupSkipped("其他活动任务仍在使用此工作区，已跳过")
    if record["status"] not in {"completed", "failed", "cancelled", "timed_out"}:
        raise CleanupSkipped("运行尚未进入可确认的终态，已跳过")


def _check_expired(payload: dict, record: dict, days: int, now: float) -> None:
    """新保留期作用于已有目录，结束时间较晚时不能提前回收。"""

    try:
        retained_at = float(payload["retained_at"])
        finished_at = float(record["finished_at"])
        started_at = float(record["started_at"])
        if not all(math.isfinite(value) and value > 0 for value in (retained_at, finished_at, started_at)):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise CleanupSkipped("无法确认运行结束或保留时间，已跳过") from None
    if retained_at < started_at:
        raise CleanupSkipped("保留标记早于本次运行，已跳过")
    if max(retained_at, finished_at) + days * 86400 > now:
        raise CleanupSkipped("尚未超过保留期")


def _directory_bytes(target: Path) -> int:
    """统计不跟随链接的文件逻辑大小，仅作为回收空间估算。"""

    def unreadable(error: OSError) -> None:
        """无法遍历时明确失败，避免静默低估或遗漏安全检查。"""

        raise error

    total = 0
    for parent, directories, files in os.walk(target, followlinks=False, onerror=unreadable):
        safe = []
        for name in directories:
            path = Path(parent) / name
            if not path.is_symlink() and not _is_junction(path.lstat()):
                safe.append(name)
        directories[:] = safe
        for name in files:
            path = Path(parent) / name
            if not path.is_symlink():
                total += path.stat().st_size
    return total


class WorkspaceCleanupManager:
    """独立于 Agent 调度的清理循环，计划及最近结果可跨服务重启查看。"""

    def __init__(self, get_config: Callable[[], AppConfig], store: StateStore) -> None:
        self.get_config = get_config
        self.store = store
        self.state = store.get_service_state(STATE_KEY) or {}
        self.running = False
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.maintenance_check: Callable[[], bool] = lambda: False

    def start(self) -> None:
        """首次启动只安排未来计划，不补跑已错过的时刻。"""

        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="teamwork-workspace-cleanup")

    def notify_config_changed(self) -> None:
        """只唤醒计划计算，不立即执行清理。"""

        self._wake.set()

    async def close(self) -> None:
        """等待当前文件操作结束，再停止后续目录清理。"""

        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    def _signature(self) -> str:
        """只有计划或本机时区变化才重排，修改保留期不推迟原计划。"""

        return stable_hash(self.get_config().runtime.workspace_cleanup.model_dump(), time.tzname, time.timezone)

    def _persist(self) -> None:
        """持久化有界摘要，不复制命令输出或凭据。"""

        self.store.set_service_state(STATE_KEY, self.state)

    def sync_plan(self, now: float, *, startup: bool = False) -> None:
        """恢复尚未到期的计划，启动时跳过停机期间的周期。"""

        schedule = self.get_config().runtime.workspace_cleanup
        changed = self.state.get("signature") != self._signature()
        due = self.state.get("next_run_at")
        if changed or (schedule.enabled and (due is None or (startup and due <= now))):
            self.state["signature"] = self._signature()
            self.state["next_run_at"] = next_workspace_cleanup_at(schedule, now) if schedule.enabled else None
            self._persist()
        if startup and self.state.get("last_run", {}).get("status") == "running":
            self.state["last_run"].update(status="interrupted", finished_at=now)
            self._persist()

    def snapshot(self) -> dict[str, Any]:
        """时间文本在服务端按本地时间格式化，不由浏览器时区重解释。"""

        next_run = self.state.get("next_run_at")
        if self.state.get("signature") != self._signature():
            schedule = self.get_config().runtime.workspace_cleanup
            next_run = next_workspace_cleanup_at(schedule, time.time()) if schedule.enabled else None
        last = self.state.get("last_run")
        format_time = lambda value: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value)) if value else None
        return {
            "running": self.running, "next_run_at": next_run, "next_run_text": format_time(next_run),
            "scheduler_error": self.state.get("scheduler_error"),
            "last_run": {**last, "started_text": format_time(last.get("started_at")),
                         "finished_text": format_time(last.get("finished_at"))} if last else None,
        }

    async def _loop(self) -> None:
        """周期不重叠，配置变化及时生效；失败只记录结果，不紧密重试。"""

        startup = True
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.sync_plan(time.time(), startup=startup)
                startup = False
                if self.state.pop("scheduler_error", None) is not None:
                    self._persist()
                due = self.state.get("next_run_at")
                if due is not None and due <= time.time():
                    self.state["next_run_at"] = next_workspace_cleanup_at(
                        self.get_config().runtime.workspace_cleanup, time.time(),
                    )
                    self._persist()
                    await self.run_once(due)
                    continue
                delay = min(60.0, max(0.1, due - time.time())) if due is not None else 60.0
            except Exception as exc:
                # 数据库短暂不可用不能让后台循环永久退出，也不能紧密重试删除。
                message = f"清理调度暂不可用（{type(exc).__name__}），稍后重新检查计划"
                self.state["scheduler_error"] = message
                LOGGER.error(message)
                delay = 60.0
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except TimeoutError:
                pass

    async def run_once(self, scheduled_at: float) -> None:
        """单批按目录让出事件循环，跨进程租约防止同一计划重复删除。"""

        owner = f"workspace-cleanup:{uuid.uuid4()}"
        try:
            async with ResourceLease(self.store, [STATE_KEY], owner, ttl_seconds=120, timeout_seconds=0) as lease:
                previous = (self.store.get_service_state(STATE_KEY) or {}).get("last_run", {})
                if previous.get("signature") == self._signature() and previous.get("scheduled_at", 0) >= scheduled_at:
                    return
                await self._run_batch(scheduled_at, owner, lease)
        except LockTimeoutError:
            # 另一服务实例正在清理，由它负责写入本批结果。
            return

    async def _run_batch(self, scheduled_at: float, owner: str, batch_lease: ResourceLease) -> None:
        """记录本批所有统计，单个目录失败不阻断其他目录。"""

        self.running = True
        config = self.get_config().model_copy(deep=True)
        report: dict[str, Any] = {
            "signature": self._signature(), "scheduled_at": scheduled_at,
            "started_at": time.time(), "status": "running", "scanned": 0,
            "removed": 0, "skipped": 0, "failed": 0, "reclaimed_bytes": 0, "details": [],
        }
        self.state["last_run"] = report

        def add_outcome(outcome: dict) -> None:
            """有界保存详情，完整统计不受显示条数限制。"""

            report["scanned"] += 1
            report[outcome["status"]] += 1
            report["reclaimed_bytes"] += outcome.get("size_bytes", 0)
            if len(report["details"]) < MAX_DETAILS:
                report["details"].append(outcome)

        try:
            self._persist()
            if not config.runtime.workspace_cleanup.enabled:
                raise CleanupSkipped("定时清理已停用，本次计划跳过")
            if self.maintenance_check():
                raise CleanupSkipped("仓库正在迁移，本次计划跳过")
            root = config.database.path.parent.resolve() / "worktrees"
            problems: list[dict] = []
            markers = await asyncio.to_thread(cleanup_markers, root, problems)
            for problem in problems:
                add_outcome(problem)
            for marker in markers:
                if self._stop.is_set() or batch_lease.lost:
                    report["status"] = "interrupted"
                    break
                outcome = await self._clean_one(config, root, marker, owner, batch_lease)
                add_outcome(outcome)
                self._persist()
                await asyncio.sleep(0)
            if report["status"] == "running":
                report["status"] = "partial" if report["failed"] else "completed"
        except CleanupSkipped as exc:
            report.update(status="skipped", reason=str(exc))
        except Exception as exc:
            report.update(status="failed", reason=f"清理检查失败（{type(exc).__name__}）")
        finally:
            self.running = False
            report["finished_at"] = time.time()
            self._persist()

    async def _clean_one(self, config: AppConfig, root: Path, marker: Path, owner: str, batch_lease: ResourceLease) -> dict:
        """取得使用租约后再次检查；权限错误和安全跳过具有不同可见状态。"""

        outcome: dict[str, Any] = {"path": str(marker.parent / marker.name[1:-len('.retained.json')])}
        record = None
        try:
            target, payload, record = await asyncio.to_thread(_inspect_marker, root, marker, self.store)
            _check_inactive(self.store, target, record)
            _check_expired(payload, record, config.runtime.worktree_retention_days, time.time())
            source = next((repo.workspace for repo in config.repositories if stable_hash(repo.id)[:16] == target.parent.name), None)
            keys = [workspace_usage_lock_key(target)]
            if source is not None:
                keys.append(f"git_repository:{source.resolve()}")
            async with ResourceLease(self.store, keys, owner, ttl_seconds=120, timeout_seconds=0) as lease:
                def perform() -> int:
                    """耗时目录遍历与删除在线程执行，删除前重新核验活动状态。"""

                    if self._stop.is_set() or lease.lost or batch_lease.lost or self.maintenance_check():
                        raise CleanupSkipped("服务停止、锁失效或仓库迁移，已跳过")
                    current = self.get_config().runtime
                    if current.workspace_cleanup != config.runtime.workspace_cleanup or current.worktree_retention_days != config.runtime.worktree_retention_days:
                        raise CleanupSkipped("清理配置已变化，等待下次计划")
                    target, payload, fresh = _inspect_marker(root, marker, self.store)
                    _check_inactive(self.store, target, fresh)
                    _check_expired(payload, fresh, current.worktree_retention_days, time.time())
                    if not target.exists():
                        marker.unlink()
                        return 0
                    size = _directory_bytes(target)
                    _, payload, fresh = _inspect_marker(root, marker, self.store)
                    _check_inactive(self.store, target, fresh)
                    _check_expired(payload, fresh, current.worktree_retention_days, time.time())
                    if self._stop.is_set() or lease.lost or batch_lease.lost or self.maintenance_check():
                        raise CleanupSkipped("服务停止、锁失效或仓库迁移，已跳过")
                    latest = self.get_config().runtime
                    if latest.workspace_cleanup != config.runtime.workspace_cleanup or latest.worktree_retention_days != config.runtime.worktree_retention_days:
                        raise CleanupSkipped("清理配置已变化，等待下次计划")
                    if (target / ".git").is_dir():
                        _plain_directory(target / ".git")
                    remove_expired_run_workspace(source, target)
                    return size

                size = await asyncio.to_thread(perform)
                outcome.update(status="removed", size_bytes=size, reason="超过保留期，工作区已清理")
                # 成功状态也在使用租约内回写，避免重试重新创建目录后被旧清理结果覆盖。
                return await self._record_outcome(record, outcome)
        except (CleanupSkipped, LockTimeoutError, LockCancelledError) as exc:
            reason = str(exc) if isinstance(exc, CleanupSkipped) else "工作区资源正在使用，已跳过"
            outcome.update(status="skipped", reason=reason)
        except WorkspaceError as exc:
            outcome.update(status="skipped", reason=str(exc))
        except PermissionError:
            outcome.update(status="failed", reason="目录权限不足或文件被占用，删除失败")
        except Exception as exc:
            outcome.update(status="failed", reason=f"清理失败（{type(exc).__name__}）")
        return await self._record_outcome(record, outcome)

    async def _record_outcome(self, record: dict | None, outcome: dict) -> dict:
        """写回关联运行；元数据故障不能掩盖真实删除结果或中断其他目录。"""

        if record is not None and outcome.get("reason") != "尚未超过保留期":
            try:
                await asyncio.to_thread(self.store.record_workspace_cleanup, record["run_id"], outcome["path"], outcome)
            except Exception as exc:
                # 文件删除与 SQLite 无法组成事务；保留实际删除结果并明确报告回写故障。
                outcome.update(status="failed", reason=f"{outcome['reason']}；运行记录回写失败（{type(exc).__name__}）")
        return outcome
