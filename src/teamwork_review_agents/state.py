"""SQLite 状态、事件、运行审计与资源租约。"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import AgentResult, ChangeEvent, ChangeRequestSnapshot


@dataclass(frozen=True)
class RunReservation:
    """一次新建或重试的 Agent 运行占位。"""

    run_id: str
    root_run_id: str
    parent_run_id: str | None
    attempts: int


class StateStore:
    """每个方法使用独立连接，以支持主进程和 MCP 子进程共享。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        """创建启用 WAL 与字典行访问的数据库连接。"""

        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        """创建数据库目录和全部基础表。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_inbox (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_event_inbox_status
                ON event_inbox(status, updated_at);

                CREATE TABLE IF NOT EXISTS event_agent_dispatches (
                    event_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    rule_name TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(event_id, idempotency_key),
                    FOREIGN KEY(event_id) REFERENCES event_inbox(event_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_event_agent_dispatches_key
                ON event_agent_dispatches(idempotency_key);

                CREATE TABLE IF NOT EXISTS provider_activity_cursors (
                    provider TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    cursor TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(provider, repository_id, number)
                );

                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    root_run_id TEXT NOT NULL,
                    parent_run_id TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    event_id TEXT,
                    rule_name TEXT,
                    agent_name TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    prompt TEXT NOT NULL,
                    environment TEXT NOT NULL DEFAULT '{}',
                    config_revision TEXT,
                    final_message TEXT,
                    thread_id TEXT,
                    usage TEXT,
                    events TEXT,
                    error TEXT,
                    workspace_path TEXT,
                    workspace_status TEXT,
                    workspace_reason TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    started_at REAL NOT NULL,
                    finished_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_agent_runs_root
                ON agent_runs(root_run_id, started_at);

                CREATE TABLE IF NOT EXISTS run_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    stream TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_run_logs_cursor
                ON run_logs(run_id, id);

                CREATE TABLE IF NOT EXISTS config_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    revision TEXT NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS service_state (
                    state_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS resource_locks (
                    resource_key TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    hold_count INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            self._ensure_column(
                connection,
                "agent_runs",
                "environment",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                connection,
                "agent_runs",
                "config_revision",
                "TEXT",
            )
            self._ensure_column(connection, "agent_runs", "workspace_path", "TEXT")
            self._ensure_column(connection, "agent_runs", "workspace_status", "TEXT")
            self._ensure_column(connection, "agent_runs", "workspace_reason", "TEXT")
            self._ensure_column(
                connection,
                "agent_runs",
                "cancel_requested",
                "INTEGER NOT NULL DEFAULT 0",
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        """为已有 SQLite 数据库执行轻量兼容迁移。"""

        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def recover_interrupted_work(self) -> None:
        """单实例服务启动时恢复上次异常退出遗留的未完成状态。"""

        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE event_inbox
                SET status = 'pending', error = '服务异常退出，事件已重新入队', updated_at = ?
                WHERE status IN ('processing', 'triggered')
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE agent_runs
                SET status = 'failed', error = '服务异常退出，运行未正常结束',
                    workspace_status = CASE
                        WHEN workspace_status = 'active' THEN 'retained'
                        ELSE workspace_status
                    END,
                    workspace_reason = CASE
                        WHEN workspace_status = 'active'
                        THEN '服务异常退出，保留临时工作区用于恢复'
                        ELSE workspace_reason
                    END,
                    finished_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (now,),
            )
            connection.execute("DELETE FROM resource_locks")
            connection.commit()

    def load_snapshot(self, snapshot_key: str) -> ChangeRequestSnapshot | None:
        """读取上一次扫描的变更请求快照。"""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM snapshots WHERE snapshot_key = ?",
                (snapshot_key,),
            ).fetchone()
        if row is None:
            return None
        return ChangeRequestSnapshot.model_validate_json(row["payload"])

    def load_activity_cursor(
        self,
        provider: str,
        repository_id: str,
        number: int,
    ) -> dict[str, Any] | None:
        """读取 Provider 为单个 MR/PR 保存的不透明活动游标。"""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT cursor FROM provider_activity_cursors
                WHERE provider = ? AND repository_id = ? AND number = ?
                """,
                (provider, repository_id, number),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["cursor"])
        return value if isinstance(value, dict) else None

    def snapshots_without_activity_cursor(
        self,
        provider: str,
        repository_id: str,
        *,
        limit: int = 100,
    ) -> list[ChangeRequestSnapshot]:
        """返回已有快照中尚未建立 Provider 活动基线的项目。"""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT snapshot.payload
                FROM snapshots AS snapshot
                LEFT JOIN provider_activity_cursors AS activity
                  ON activity.provider = ?
                 AND activity.repository_id = ?
                 AND activity.number = json_extract(snapshot.payload, '$.number')
                WHERE json_extract(snapshot.payload, '$.provider') = ?
                  AND json_extract(snapshot.payload, '$.repository_id') = ?
                  AND activity.provider IS NULL
                ORDER BY snapshot.updated_at ASC
                LIMIT ?
                """,
                (provider, repository_id, provider, repository_id, limit),
            ).fetchall()
        return [
            ChangeRequestSnapshot.model_validate_json(row["payload"])
            for row in rows
        ]

    def save_activity_cursor(
        self,
        provider: str,
        repository_id: str,
        number: int,
        cursor: dict[str, Any],
    ) -> None:
        """单独保存首次初始化的 Provider 活动游标。"""

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_activity_cursors (
                    provider, repository_id, number, cursor, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider, repository_id, number) DO UPDATE SET
                    cursor = excluded.cursor,
                    updated_at = excluded.updated_at
                """,
                (
                    provider,
                    repository_id,
                    number,
                    json.dumps(cursor, ensure_ascii=False),
                    time.time(),
                ),
            )

    @staticmethod
    def _insert_events(
        connection: sqlite3.Connection,
        events: Iterable[ChangeEvent],
        now: float,
    ) -> int:
        """在已有事务中幂等写入事件并返回新增数量。"""

        inserted = 0
        for event in events:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO event_inbox (
                    event_id, event_type, repository_id, number, payload,
                    status, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    event.id,
                    event.type,
                    event.repository_id,
                    event.number,
                    event.model_dump_json(),
                    now,
                    now,
                ),
            )
            inserted += cursor.rowcount
        return inserted

    def enqueue_events(self, events: Iterable[ChangeEvent]) -> int:
        """不改写快照，幂等追加管理员补发或其他外部产生的事件。"""

        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = self._insert_events(connection, events, now)
            connection.commit()
        return inserted

    def has_event_type(
        self,
        repository_id: str,
        number: int,
        event_type: str,
    ) -> bool:
        """判断指定 MR/PR 是否已经产生过某类事件。"""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM event_inbox
                WHERE repository_id = ? AND number = ? AND event_type = ?
                LIMIT 1
                """,
                (repository_id, number, event_type),
            ).fetchone()
        return row is not None

    def list_snapshots(
        self,
        limit: int = 100,
        *,
        repository_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """返回已扫描 MR/PR 的最新快照摘要，不暴露平台原始响应。"""

        where = "WHERE json_extract(payload, '$.repository_id') = ?" if repository_id else ""
        parameters: list[Any] = [repository_id] if repository_id else []
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT snapshot_key, payload, updated_at,
                       EXISTS(
                           SELECT 1 FROM event_inbox event
                           WHERE event.repository_id = json_extract(snapshots.payload, '$.repository_id')
                             AND event.number = json_extract(snapshots.payload, '$.number')
                             AND event.event_type = 'change_request.discovered'
                       ) AS discovered_event_emitted
                FROM snapshots {where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            snapshot = ChangeRequestSnapshot.model_validate_json(row["payload"])
            summary = snapshot.model_dump(mode="json", exclude={"raw"})
            summary.update(
                {
                    "snapshot_key": row["snapshot_key"],
                    "scanned_at": row["updated_at"],
                    "discovered_event_emitted": bool(row["discovered_event_emitted"]),
                }
            )
            results.append(summary)
        return results

    def save_snapshot_and_events(
        self,
        snapshot: ChangeRequestSnapshot,
        events: Iterable[ChangeEvent],
        *,
        activity_cursor: dict[str, Any] | None = None,
    ) -> int:
        """在一个事务中更新快照、事件和可选 Provider 活动游标。"""

        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = self._insert_events(connection, events, now)
            connection.execute(
                """
                INSERT INTO snapshots (snapshot_key, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(snapshot_key) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (snapshot.key, snapshot.model_dump_json(), now),
            )
            if activity_cursor is not None:
                connection.execute(
                    """
                    INSERT INTO provider_activity_cursors (
                        provider, repository_id, number, cursor, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(provider, repository_id, number) DO UPDATE SET
                        cursor = excluded.cursor,
                        updated_at = excluded.updated_at
                    """,
                    (
                        snapshot.provider,
                        snapshot.repository_id,
                        snapshot.number,
                        json.dumps(activity_cursor, ensure_ascii=False),
                        now,
                    ),
                )
            connection.commit()
        return inserted

    def pending_events(self, limit: int = 100) -> list[ChangeEvent]:
        """按创建顺序读取待处理或待重试事件。"""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM event_inbox
                WHERE status IN ('pending', 'failed')
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [ChangeEvent.model_validate_json(row["payload"]) for row in rows]

    def claim_event(self, event_id: str, max_attempts: int) -> bool:
        """原子领取一个事件，并限制总尝试次数。"""

        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, attempts FROM event_inbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] not in {"pending", "failed"}
                or row["attempts"] >= max_attempts
            ):
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE event_inbox
                SET status = 'processing', attempts = attempts + 1,
                    error = NULL, updated_at = ?
                WHERE event_id = ?
                """,
                (now, event_id),
            )
            connection.commit()
        return True

    def record_event_dispatches(
        self,
        event_ids: Iterable[str],
        dispatches: Iterable[tuple[str, str, str, str]],
    ) -> None:
        """记录事件到 Agent 调度的关系，并区分未触发与已触发事件。"""

        ids = tuple(dict.fromkeys(event_ids))
        items = tuple(dispatches)
        matched_ids = {event_id for event_id, *_ in items}
        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                INSERT OR IGNORE INTO event_agent_dispatches (
                    event_id, idempotency_key, rule_name, agent_name, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [(*item, now) for item in items],
            )
            for event_id in ids:
                connection.execute(
                    """
                    UPDATE event_inbox
                    SET status = ?, error = NULL, updated_at = ?
                    WHERE event_id = ?
                    """,
                    (
                        "triggered" if event_id in matched_ids else "unmatched",
                        now,
                        event_id,
                    ),
                )
            connection.commit()

    def finish_event(
        self,
        event_id: str,
        *,
        error: str | None = None,
        status: str | None = None,
    ) -> None:
        """将事件标记为指定的终态，默认根据错误选择完成或失败。"""

        final_status = "failed" if error else status or "completed"

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE event_inbox
                SET status = ?, error = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (final_status, error, time.time(), event_id),
            )

    def begin_agent_run(
        self,
        *,
        proposed_run_id: str,
        root_run_id: str | None,
        parent_run_id: str | None,
        idempotency_key: str,
        event_id: str | None,
        rule_name: str | None,
        agent_name: str,
        resource_key: str,
        prompt: str,
        environment: dict[str, str] | None = None,
        config_revision: str | None = None,
        max_attempts: int,
    ) -> RunReservation | None:
        """幂等创建 Agent 运行，失败记录可在上限内复用重试。"""

        now = time.time()
        root = root_run_id or proposed_run_id
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT run_id, root_run_id, parent_run_id, status, attempts
                FROM agent_runs WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO agent_runs (
                        run_id, root_run_id, parent_run_id, idempotency_key,
                        event_id, rule_name, agent_name, resource_key,
                        status, attempts, prompt, environment, config_revision, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 1, ?, ?, ?, ?)
                    """,
                    (
                        proposed_run_id,
                        root,
                        parent_run_id,
                        idempotency_key,
                        event_id,
                        rule_name,
                        agent_name,
                        resource_key,
                        prompt,
                        json.dumps(environment or {}, ensure_ascii=False),
                        config_revision,
                        now,
                    ),
                )
                connection.commit()
                return RunReservation(proposed_run_id, root, parent_run_id, 1)

            if row["status"] not in {"failed", "timed_out"} or row["attempts"] >= max_attempts:
                connection.rollback()
                return None
            attempts = int(row["attempts"]) + 1
            connection.execute(
                """
                UPDATE agent_runs
                SET status = 'queued', attempts = ?, prompt = ?, environment = ?,
                    config_revision = ?, error = NULL,
                    final_message = NULL, events = NULL, usage = NULL,
                    workspace_path = NULL, workspace_status = NULL,
                    workspace_reason = NULL, cancel_requested = 0,
                    started_at = ?, finished_at = NULL
                WHERE run_id = ?
                """,
                (
                    attempts,
                    prompt,
                    json.dumps(environment or {}, ensure_ascii=False),
                    config_revision,
                    now,
                    row["run_id"],
                ),
            )
            connection.commit()
            return RunReservation(
                row["run_id"],
                row["root_run_id"],
                row["parent_run_id"],
                attempts,
            )

    def agent_run_status(self, idempotency_key: str) -> str | None:
        """查询一个幂等运行当前状态。"""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM agent_runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return None if row is None else str(row["status"])

    def mark_agent_run_running(self, run_id: str) -> bool:
        """在 Codex CLI 即将启动时把 Agent 从排队切换为执行中。"""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET status = 'running'
                WHERE run_id = ? AND status = 'queued' AND cancel_requested = 0
                """,
                (run_id,),
            )
        return cursor.rowcount == 1

    def agent_run_cancel_requested(self, run_id: str) -> bool:
        """返回某次运行是否已收到持久化取消请求。"""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def request_cancel_run(self, run_id: str) -> list[str] | None:
        """取消指定运行及全部后代，并立即结束仍在排队的运行。"""

        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if exists is None:
                connection.rollback()
                return None
            rows = connection.execute(
                """
                WITH RECURSIVE descendants(run_id) AS (
                    SELECT run_id FROM agent_runs WHERE run_id = ?
                    UNION ALL
                    SELECT child.run_id
                    FROM agent_runs AS child
                    JOIN descendants AS parent
                        ON child.parent_run_id = parent.run_id
                )
                SELECT run_id, status FROM agent_runs
                WHERE run_id IN (SELECT run_id FROM descendants)
                """,
                (run_id,),
            ).fetchall()
            active_ids = [
                str(row["run_id"])
                for row in rows
                if row["status"] in {"queued", "running"}
            ]
            if active_ids:
                placeholders = ", ".join("?" for _ in active_ids)
                connection.execute(
                    f"""
                    UPDATE agent_runs
                    SET cancel_requested = 1,
                        status = CASE
                            WHEN status = 'queued' THEN 'cancelled'
                            ELSE status
                        END,
                        error = CASE
                            WHEN status = 'queued' THEN '运行已由管理员取消'
                            ELSE error
                        END,
                        finished_at = CASE
                            WHEN status = 'queued' THEN ?
                            ELSE finished_at
                        END
                    WHERE run_id IN ({placeholders})
                    """,
                    (now, *active_ids),
                )
            connection.commit()
        return active_ids

    def update_agent_run_inputs(
        self,
        run_id: str,
        *,
        prompt: str,
        environment: dict[str, str],
        config_revision: str,
    ) -> None:
        """在重试复用历史 run_id 时更新最终渲染输入。"""

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE agent_runs
                SET prompt = ?, environment = ?, config_revision = ?
                WHERE run_id = ?
                """,
                (
                    prompt,
                    json.dumps(environment, ensure_ascii=False),
                    config_revision,
                    run_id,
                ),
            )

    def update_agent_run_workspace(
        self,
        run_id: str,
        *,
        path: str,
        status: str,
        reason: str | None = None,
    ) -> None:
        """保存本次运行的实际工作区与清理状态。"""

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE agent_runs
                SET workspace_path = ?, workspace_status = ?, workspace_reason = ?
                WHERE run_id = ?
                """,
                (path, status, reason, run_id),
            )

    def finish_agent_run(self, result: AgentResult) -> None:
        """保存 Codex CLI 最终结果与截断后的 JSONL 事件。"""

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE agent_runs
                SET status = ?, final_message = ?, thread_id = ?, usage = ?,
                    events = ?, error = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (
                    result.status,
                    result.final_message,
                    result.thread_id,
                    json.dumps(result.usage, ensure_ascii=False),
                    json.dumps(result.events, ensure_ascii=False),
                    result.error,
                    time.time(),
                    result.run_id,
                ),
            )

    def count_root_runs(self, root_run_id: str) -> int:
        """统计一个根任务下创建的不同 Agent 运行数。"""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM agent_runs WHERE root_run_id = ?",
                (root_run_id,),
            ).fetchone()
        return int(row["count"])

    def acquire_locks(
        self,
        resource_keys: Iterable[str],
        owner: str,
        ttl_seconds: int,
    ) -> bool:
        """原子申请多个可重入资源租约。"""

        keys = sorted(set(resource_keys))
        if not keys:
            return True
        now = time.time()
        expires_at = now + ttl_seconds
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM resource_locks WHERE expires_at <= ?", (now,))
            for key in keys:
                row = connection.execute(
                    "SELECT owner FROM resource_locks WHERE resource_key = ?",
                    (key,),
                ).fetchone()
                if row is not None and row["owner"] != owner:
                    connection.rollback()
                    return False
            for key in keys:
                connection.execute(
                    """
                    INSERT INTO resource_locks (
                        resource_key, owner, hold_count, expires_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(resource_key) DO UPDATE SET
                        hold_count = hold_count + 1,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (key, owner, expires_at, now),
                )
            connection.commit()
        return True

    def renew_locks(
        self,
        resource_keys: Iterable[str],
        owner: str,
        ttl_seconds: int,
    ) -> bool:
        """续期当前所有者持有的资源租约。"""

        keys = sorted(set(resource_keys))
        if not keys:
            return True
        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for key in keys:
                row = connection.execute(
                    "SELECT owner FROM resource_locks WHERE resource_key = ?",
                    (key,),
                ).fetchone()
                if row is None or row["owner"] != owner:
                    connection.rollback()
                    return False
            connection.executemany(
                """
                UPDATE resource_locks SET expires_at = ?, updated_at = ?
                WHERE resource_key = ? AND owner = ?
                """,
                [(now + ttl_seconds, now, key, owner) for key in keys],
            )
            connection.commit()
        return True

    def release_locks(self, resource_keys: Iterable[str], owner: str) -> None:
        """释放一次可重入持有计数。"""

        keys = sorted(set(resource_keys))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for key in keys:
                row = connection.execute(
                    """
                    SELECT hold_count FROM resource_locks
                    WHERE resource_key = ? AND owner = ?
                    """,
                    (key, owner),
                ).fetchone()
                if row is None:
                    continue
                if row["hold_count"] <= 1:
                    connection.execute(
                        "DELETE FROM resource_locks WHERE resource_key = ? AND owner = ?",
                        (key, owner),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE resource_locks
                        SET hold_count = hold_count - 1, updated_at = ?
                        WHERE resource_key = ? AND owner = ?
                        """,
                        (time.time(), key, owner),
                    )
            connection.commit()

    def append_run_log(
        self,
        run_id: str,
        *,
        stream: str,
        event_type: str,
        payload: str | dict[str, Any] | list[Any],
    ) -> int:
        """追加一条可供 SSE 按游标读取的运行日志。"""

        encoded = (
            payload
            if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False)
        )
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO run_logs (
                    run_id, created_at, stream, event_type, payload
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, time.time(), stream, event_type, encoded),
            )
        return int(cursor.lastrowid)

    def list_run_logs(
        self,
        run_id: str,
        *,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """返回某次运行在指定游标之后的日志。"""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, created_at, stream, event_type, payload
                FROM run_logs
                WHERE run_id = ? AND id > ?
                ORDER BY id ASC LIMIT ?
                """,
                (run_id, after_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """返回运行详情和直属 sub-agent。"""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT agent_runs.*, event_inbox.repository_id,
                       event_inbox.number AS change_request_number,
                       event_inbox.payload AS event_payload
                FROM agent_runs
                LEFT JOIN event_inbox ON event_inbox.event_id = agent_runs.event_id
                WHERE agent_runs.run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            children = connection.execute(
                """
                SELECT run_id, agent_name, status, started_at, finished_at,
                       workspace_path, workspace_status, workspace_reason
                FROM agent_runs WHERE parent_run_id = ? ORDER BY started_at ASC
                """,
                (run_id,),
            ).fetchall()
        result = self._decorate_run_record(dict(row))
        for key in ("environment", "usage"):
            if result.get(key):
                result[key] = json.loads(result[key])
        result.pop("events", None)
        result["children"] = [dict(child) for child in children]
        return result

    @staticmethod
    def _decorate_run_record(record: dict[str, Any]) -> dict[str, Any]:
        """从关联事件中提取运行列表需要的 MR/PR 摘要。"""

        event_payload = record.pop("event_payload", None)
        if not event_payload:
            return record
        try:
            event = json.loads(event_payload)
        except (TypeError, json.JSONDecodeError):
            return record
        snapshot = event.get("current") or event.get("new") or {}
        record["change_request_title"] = snapshot.get("title")
        record["change_request_url"] = snapshot.get("web_url")
        return record

    def list_runs(
        self,
        limit: int = 20,
        *,
        status: str | None = None,
        agent_name: str | None = None,
        repository_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """按可选条件返回最近 Agent 运行摘要。"""

        conditions: list[str] = []
        parameters: list[Any] = []
        if status:
            conditions.append("agent_runs.status = ?")
            parameters.append(status)
        if agent_name:
            conditions.append("agent_runs.agent_name = ?")
            parameters.append(agent_name)
        if repository_id:
            conditions.append("agent_runs.resource_key LIKE ?")
            parameters.append(f"%:{repository_id}:%")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.append(limit)

        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT agent_runs.run_id, agent_runs.root_run_id,
                       agent_runs.parent_run_id, agent_runs.event_id,
                       agent_runs.rule_name, agent_runs.agent_name,
                       agent_runs.resource_key, agent_runs.status,
                       agent_runs.attempts, agent_runs.error,
                       agent_runs.cancel_requested,
                       agent_runs.workspace_path, agent_runs.workspace_status,
                       agent_runs.workspace_reason, agent_runs.started_at,
                       agent_runs.finished_at, event_inbox.repository_id,
                       event_inbox.number AS change_request_number,
                       event_inbox.payload AS event_payload
                FROM agent_runs
                LEFT JOIN event_inbox ON event_inbox.event_id = agent_runs.event_id
                {where} ORDER BY agent_runs.started_at DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._decorate_run_record(dict(row)) for row in rows]

    def dashboard_stats(self) -> dict[str, Any]:
        """返回管理首页需要的运行与事件统计。"""

        with self.connect() as connection:
            run_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM agent_runs GROUP BY status"
            ).fetchall()
            event_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM event_inbox GROUP BY status"
            ).fetchall()
            snapshot_rows = connection.execute(
                "SELECT payload FROM snapshots"
            ).fetchall()
        change_requests: dict[str, int] = {"total": len(snapshot_rows)}
        for row in snapshot_rows:
            state = ChangeRequestSnapshot.model_validate_json(row["payload"]).state
            change_requests[state] = change_requests.get(state, 0) + 1
        return {
            "runs": {row["status"]: row["count"] for row in run_rows},
            "events": {row["status"]: row["count"] for row in event_rows},
            "change_requests": change_requests,
        }

    def list_events(
        self,
        limit: int = 50,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """返回最近 MR/PR 语义事件。"""

        where = "WHERE event_inbox.status = ?" if status else ""
        parameters: list[Any] = [status] if status else []
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT event_inbox.event_id, event_inbox.event_type,
                       event_inbox.repository_id, event_inbox.number,
                       event_inbox.status, event_inbox.attempts,
                       event_inbox.error, event_inbox.created_at,
                       event_inbox.updated_at,
                       COALESCE(dispatch_stats.trigger_count, 0) AS trigger_count,
                       COALESCE(dispatch_stats.agent_queued_count, 0)
                           AS agent_queued_count,
                       COALESCE(dispatch_stats.agent_running_count, 0)
                           AS agent_running_count,
                       COALESCE(dispatch_stats.agent_completed_count, 0)
                           AS agent_completed_count,
                       COALESCE(dispatch_stats.agent_failed_count, 0)
                           AS agent_failed_count,
                       COALESCE(dispatch_stats.agent_timed_out_count, 0)
                           AS agent_timed_out_count,
                       COALESCE(dispatch_stats.agent_cancelled_count, 0)
                           AS agent_cancelled_count
                FROM event_inbox
                LEFT JOIN (
                    SELECT dispatch.event_id,
                           COUNT(*) AS trigger_count,
                           SUM(
                               CASE
                                   WHEN run.run_id IS NULL OR run.status = 'queued'
                                   THEN 1 ELSE 0
                               END
                           ) AS agent_queued_count,
                           SUM(CASE WHEN run.status = 'running' THEN 1 ELSE 0 END)
                               AS agent_running_count,
                           SUM(CASE WHEN run.status = 'completed' THEN 1 ELSE 0 END)
                               AS agent_completed_count,
                           SUM(CASE WHEN run.status = 'failed' THEN 1 ELSE 0 END)
                               AS agent_failed_count,
                           SUM(CASE WHEN run.status = 'timed_out' THEN 1 ELSE 0 END)
                               AS agent_timed_out_count,
                           SUM(CASE WHEN run.status = 'cancelled' THEN 1 ELSE 0 END)
                               AS agent_cancelled_count
                    FROM event_agent_dispatches AS dispatch
                    LEFT JOIN agent_runs AS run
                        ON run.idempotency_key = dispatch.idempotency_key
                    GROUP BY dispatch.event_id
                ) AS dispatch_stats
                    ON dispatch_stats.event_id = event_inbox.event_id
                {where} ORDER BY event_inbox.created_at DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def save_config_version(
        self,
        revision: str,
        content: str,
        source: str,
    ) -> None:
        """幂等保存一版有效配置。"""

        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO config_versions (
                    revision, content, source, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (revision, content, source, time.time()),
            )

    def list_config_versions(self, limit: int = 20) -> list[dict[str, Any]]:
        """返回最近配置版本，不默认携带完整内容。"""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, revision, source, created_at
                FROM config_versions ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_config_version(self, revision: str) -> dict[str, Any] | None:
        """按 revision 返回配置历史内容。"""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT revision, content, source, created_at
                FROM config_versions WHERE revision = ?
                """,
                (revision,),
            ).fetchone()
        return None if row is None else dict(row)

    def set_service_state(self, key: str, payload: dict[str, Any]) -> None:
        """更新后台服务心跳或控制状态。"""

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO service_state (state_key, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(payload, ensure_ascii=False), time.time()),
            )

    def get_service_state(self, key: str) -> dict[str, Any] | None:
        """读取后台服务状态。"""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload, updated_at FROM service_state WHERE state_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload["updated_at"] = row["updated_at"]
        return payload

    def prune_run_logs(self, older_than: float) -> int:
        """删除超过保留期的运行日志。"""

        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM run_logs WHERE created_at < ?",
                (older_than,),
            )
        return cursor.rowcount
