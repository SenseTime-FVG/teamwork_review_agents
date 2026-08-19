"""SQLite 状态、事件、运行审计与资源租约。"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .events import TARGET_COMMITS_CHANGED_EVENT, activity_event_type
from .models import (
    AgentResult,
    ChangeEvent,
    ChangeRequestActivity,
    ChangeRequestSnapshot,
    PreflightResult,
)


CANCEL_SOURCE_ADMINISTRATOR = "administrator"
CANCEL_SOURCE_SERVICE_SHUTDOWN = "service_shutdown"
LEGACY_CANCELLED_RETRY_ERROR = (
    "幂等任务已经达到重试上限，当前状态：cancelled"
)


@dataclass(frozen=True)
class RunReservation:
    """一次新建或重试的 Agent 运行占位。"""

    run_id: str
    root_run_id: str
    parent_run_id: str | None
    attempts: int


@dataclass(frozen=True)
class PreflightReservation:
    """一次新建或基础设施错误重试的 CI 运行占位。"""

    run_id: str
    attempts: int


class StateStore:
    """每个方法使用独立连接，以支持主进程和 MCP 子进程共享。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """创建启用 WAL 的连接，并在事务结束后确定关闭。"""

        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            with connection:
                yield connection
        finally:
            connection.close()

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
                    queue_reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_event_inbox_status
                ON event_inbox(status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_event_inbox_change_request
                ON event_inbox(repository_id, number, created_at DESC);

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
                    repository_id TEXT,
                    change_request_number INTEGER,
                    change_request_title TEXT,
                    change_request_url TEXT,
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
                    cancel_source TEXT,
                    concurrency_acquired INTEGER NOT NULL DEFAULT 0,
                    queue_reason TEXT,
                    started_at REAL NOT NULL,
                    finished_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_agent_runs_root
                ON agent_runs(root_run_id, started_at);

                CREATE TABLE IF NOT EXISTS preflight_runs (
                    run_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    event_id TEXT,
                    repository_id TEXT NOT NULL,
                    number INTEGER,
                    head_sha TEXT NOT NULL,
                    config_revision TEXT NOT NULL,
                    trigger_source TEXT NOT NULL DEFAULT 'event',
                    branch TEXT,
                    phase TEXT NOT NULL DEFAULT 'queued',
                    cache_path TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    failed_step TEXT,
                    exit_code INTEGER,
                    output TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    status_published INTEGER NOT NULL DEFAULT 0,
                    started_at REAL NOT NULL,
                    finished_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_preflight_runs_change_request
                ON preflight_runs(repository_id, number, head_sha);

                CREATE INDEX IF NOT EXISTS idx_preflight_runs_event_started
                ON preflight_runs(event_id, started_at DESC);

                CREATE INDEX IF NOT EXISTS idx_preflight_runs_started
                ON preflight_runs(started_at DESC);

                CREATE TABLE IF NOT EXISTS preflight_step_runs (
                    run_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    command TEXT NOT NULL,
                    status TEXT NOT NULL,
                    timeout_seconds REAL,
                    started_at REAL,
                    finished_at REAL,
                    exit_code INTEGER,
                    error TEXT,
                    PRIMARY KEY(run_id, step_index),
                    FOREIGN KEY(run_id) REFERENCES preflight_runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_preflight_step_runs_status
                ON preflight_step_runs(run_id, status, step_index);

                CREATE TABLE IF NOT EXISTS preflight_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    stream TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES preflight_runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_preflight_logs_cursor
                ON preflight_logs(run_id, id);

                CREATE TABLE IF NOT EXISTS preflight_failure_comments (
                    repository_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    status_context TEXT NOT NULL,
                    remote_comment_id TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(repository_id, number)
                );

                CREATE TABLE IF NOT EXISTS event_preflight_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    reused INTEGER NOT NULL DEFAULT 0,
                    linked_at REAL NOT NULL,
                    UNIQUE(event_id, run_id),
                    FOREIGN KEY(event_id) REFERENCES event_inbox(event_id) ON DELETE CASCADE,
                    FOREIGN KEY(run_id) REFERENCES preflight_runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_event_preflight_links_event
                ON event_preflight_links(event_id, linked_at DESC);

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
            self._migrate_preflight_runs_for_manual(connection)
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
            self._ensure_column(connection, "agent_runs", "repository_id", "TEXT")
            self._ensure_column(
                connection,
                "agent_runs",
                "change_request_number",
                "INTEGER",
            )
            self._ensure_column(
                connection,
                "agent_runs",
                "change_request_title",
                "TEXT",
            )
            self._ensure_column(
                connection,
                "agent_runs",
                "change_request_url",
                "TEXT",
            )
            self._ensure_column(
                connection,
                "agent_runs",
                "cancel_requested",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "agent_runs",
                "concurrency_acquired",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "agent_runs", "queue_reason", "TEXT")
            self._ensure_column(connection, "event_inbox", "queue_reason", "TEXT")
            cancel_source_added = self._ensure_column(
                connection,
                "agent_runs",
                "cancel_source",
                "TEXT",
            )
            self._ensure_column(
                connection,
                "preflight_runs",
                "status_published",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "preflight_runs",
                "trigger_source",
                "TEXT NOT NULL DEFAULT 'event'",
            )
            self._ensure_column(connection, "preflight_runs", "branch", "TEXT")
            self._ensure_column(
                connection,
                "preflight_runs",
                "phase",
                "TEXT NOT NULL DEFAULT 'queued'",
            )
            self._ensure_column(connection, "preflight_runs", "cache_path", "TEXT")
            self._ensure_column(
                connection,
                "preflight_runs",
                "cancel_requested",
                "INTEGER NOT NULL DEFAULT 0",
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO event_preflight_links (
                    event_id, run_id, reused, linked_at
                )
                SELECT preflight.event_id, preflight.run_id, 0,
                       preflight.started_at
                FROM preflight_runs AS preflight
                JOIN event_inbox AS event
                    ON event.event_id = preflight.event_id
                """
            )
            if cancel_source_added:
                self._recover_legacy_shutdown_cancellations(connection)

    @staticmethod
    def _migrate_preflight_runs_for_manual(
        connection: sqlite3.Connection,
    ) -> None:
        """把旧版强制绑定事件和编号的 CI 表迁移为通用运行表。"""

        columns = {
            str(row["name"]): row
            for row in connection.execute(
                "PRAGMA table_info(preflight_runs)"
            ).fetchall()
        }
        if not columns or (
            not int(columns["event_id"]["notnull"])
            and not int(columns["number"]["notnull"])
        ):
            return
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE preflight_runs_migrated (
                    run_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    event_id TEXT,
                    repository_id TEXT NOT NULL,
                    number INTEGER,
                    head_sha TEXT NOT NULL,
                    config_revision TEXT NOT NULL,
                    trigger_source TEXT NOT NULL DEFAULT 'event',
                    branch TEXT,
                    phase TEXT NOT NULL DEFAULT 'finished',
                    cache_path TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    failed_step TEXT,
                    exit_code INTEGER,
                    output TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    status_published INTEGER NOT NULL DEFAULT 0,
                    started_at REAL NOT NULL,
                    finished_at REAL
                );
                INSERT INTO preflight_runs_migrated (
                    run_id, idempotency_key, event_id, repository_id, number,
                    head_sha, config_revision, trigger_source, phase, status,
                    attempts, failed_step, exit_code, output, error,
                    status_published, started_at, finished_at
                )
                SELECT run_id, idempotency_key, event_id, repository_id, number,
                       head_sha, config_revision, 'event',
                       CASE WHEN status = 'running' THEN 'running_steps'
                            ELSE 'finished' END,
                       status, attempts, failed_step, exit_code, output, error,
                       status_published, started_at, finished_at
                FROM preflight_runs;
                DROP TABLE preflight_runs;
                ALTER TABLE preflight_runs_migrated RENAME TO preflight_runs;
                CREATE INDEX idx_preflight_runs_change_request
                ON preflight_runs(repository_id, number, head_sha);
                CREATE INDEX idx_preflight_runs_event_started
                ON preflight_runs(event_id, started_at DESC);
                CREATE INDEX idx_preflight_runs_started
                ON preflight_runs(started_at DESC);
                COMMIT;
                """
            )
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> bool:
        """为已有 SQLite 数据库执行轻量兼容迁移。"""

        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            return True
        return False

    @staticmethod
    def _recover_legacy_shutdown_cancellations(
        connection: sqlite3.Connection,
    ) -> None:
        """一次性恢复旧版本中被服务重启耗尽重试的取消事件。"""

        candidates = connection.execute(
            """
            SELECT event_inbox.event_id
            FROM event_inbox
            JOIN event_agent_dispatches AS dispatch
                ON dispatch.event_id = event_inbox.event_id
            JOIN agent_runs AS run
                ON run.idempotency_key = dispatch.idempotency_key
            WHERE event_inbox.status = 'failed'
              AND event_inbox.error = ?
            GROUP BY event_inbox.event_id
            HAVING COUNT(*) > 0
               AND SUM(
                   CASE
                       WHEN run.status = 'cancelled'
                        AND run.attempts = 1
                        AND run.cancel_source IS NULL
                       THEN 0 ELSE 1
                   END
               ) = 0
            """,
            (LEGACY_CANCELLED_RETRY_ERROR,),
        ).fetchall()
        event_ids = [str(row["event_id"]) for row in candidates]
        if not event_ids:
            return
        placeholders = ", ".join("?" for _ in event_ids)
        now = time.time()
        connection.execute(
            f"""
            UPDATE event_inbox
            SET status = 'pending', attempts = 0,
                error = '旧版本服务重启中断，事件已重新入队', updated_at = ?
            WHERE event_id IN ({placeholders})
            """,
            (now, *event_ids),
        )
        connection.execute(
            f"""
            UPDATE agent_runs
            SET cancel_source = ?
            WHERE idempotency_key IN (
                SELECT idempotency_key
                FROM event_agent_dispatches
                WHERE event_id IN ({placeholders})
            )
              AND status = 'cancelled'
              AND attempts = 1
              AND cancel_source IS NULL
            """,
            (CANCEL_SOURCE_SERVICE_SHUTDOWN, *event_ids),
        )

    def recover_interrupted_work(self) -> None:
        """单实例服务启动时恢复上次异常退出遗留的未完成状态。"""

        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE event_inbox
                SET status = 'pending', error = '服务异常退出，事件已重新入队',
                    queue_reason = NULL, updated_at = ?
                WHERE status IN ('processing', 'triggered')
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE event_inbox SET queue_reason = NULL
                WHERE status = 'pending' AND queue_reason IS NOT NULL
                """
            )
            connection.execute(
                """
                UPDATE agent_runs
                SET status = 'failed', error = '服务异常退出，运行未正常结束',
                    concurrency_acquired = 0, queue_reason = NULL,
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
                WHERE status IN ('queued', 'preparing', 'running')
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE preflight_step_runs
                SET status = 'error',
                    error = COALESCE(error, '服务异常退出，CI 步骤未正常结束'),
                    finished_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE preflight_step_runs
                SET status = 'skipped',
                    error = COALESCE(error, '服务异常退出，步骤未执行'),
                    finished_at = ?
                WHERE status = 'pending'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE preflight_runs
                SET status = 'error', phase = 'finished',
                    error = '服务异常退出，CI 运行未正常结束', finished_at = ?
                WHERE status = 'running'
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
        """返回尚无活动游标或最新活动缓存的已有快照。"""

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
                  AND (
                      activity.provider IS NULL
                      OR COALESCE(
                          json_extract(
                              activity.cursor, '$.latest_activity_checked'
                          ),
                          0
                      ) != 1
                  )
                ORDER BY snapshot.updated_at ASC
                LIMIT ?
                """,
                (provider, repository_id, provider, repository_id, limit),
            ).fetchall()
        return [
            ChangeRequestSnapshot.model_validate_json(row["payload"])
            for row in rows
        ]

    def repository_snapshots(
        self,
        provider: str,
        repository_id: str,
    ) -> list[ChangeRequestSnapshot]:
        """返回指定仓库已经保存的全部 MR/PR 快照。"""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM snapshots
                WHERE json_extract(payload, '$.provider') = ?
                  AND json_extract(payload, '$.repository_id') = ?
                ORDER BY updated_at ASC
                """,
                (provider, repository_id),
            ).fetchall()
        return [
            ChangeRequestSnapshot.model_validate_json(row["payload"])
            for row in rows
        ]

    @staticmethod
    def _latest_activity_from_cursor(
        cursor: dict[str, Any] | None,
    ) -> ChangeRequestActivity | None:
        """从兼容游标中读取最新 Provider 活动。"""

        if not cursor or not isinstance(cursor.get("latest_activity"), dict):
            return None
        try:
            return ChangeRequestActivity.model_validate(cursor["latest_activity"])
        except (TypeError, ValueError):
            return None

    def load_latest_activity(
        self,
        provider: str,
        repository_id: str,
        number: int,
    ) -> ChangeRequestActivity | None:
        """读取指定 MR/PR 已缓存的最新可触发 Provider 活动。"""

        cursor = self.load_activity_cursor(provider, repository_id, number)
        return self._latest_activity_from_cursor(cursor)

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
            if event.type == TARGET_COMMITS_CHANGED_EVENT:
                payload = event.model_dump_json(
                    exclude={
                        "old": {"raw"},
                        "new": {"raw"},
                        "current": {"raw"},
                    }
                )
            else:
                payload = event.model_dump_json()
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
                    payload,
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
        limit: int | None = 100,
        *,
        repository_id: str | None = None,
        number: int | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """返回已扫描 MR/PR 的最新快照摘要，不暴露平台原始响应。"""

        conditions: list[str] = []
        parameters: list[Any] = []
        if repository_id:
            conditions.append(
                "json_extract(snapshots.payload, '$.repository_id') = ?"
            )
            parameters.append(repository_id)
        if number is not None:
            conditions.append(
                "CAST(json_extract(snapshots.payload, '$.number') AS INTEGER) = ?"
            )
            parameters.append(number)
        if status:
            conditions.append("json_extract(snapshots.payload, '$.state') = ?")
            parameters.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT snapshots.snapshot_key, snapshots.payload,
                       snapshots.updated_at, activity.cursor AS activity_cursor,
                       EXISTS(
                           SELECT 1 FROM event_inbox event
                           WHERE event.repository_id = json_extract(snapshots.payload, '$.repository_id')
                             AND event.number = json_extract(snapshots.payload, '$.number')
                             AND event.event_type = 'change_request.discovered'
                       ) AS discovered_event_emitted
                FROM snapshots
                LEFT JOIN provider_activity_cursors AS activity
                  ON activity.provider = json_extract(snapshots.payload, '$.provider')
                 AND activity.repository_id = json_extract(
                         snapshots.payload, '$.repository_id'
                     )
                 AND activity.number = json_extract(snapshots.payload, '$.number')
                {where}
                ORDER BY julianday(
                             json_extract(snapshots.payload, '$.updated_at')
                         ) DESC,
                         json_extract(snapshots.payload, '$.repository_id') ASC,
                         CAST(
                             json_extract(snapshots.payload, '$.number') AS INTEGER
                         ) DESC
                {limit_clause}
                """,
                parameters,
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            snapshot = ChangeRequestSnapshot.model_validate_json(row["payload"])
            activity_cursor = (
                json.loads(row["activity_cursor"])
                if row["activity_cursor"]
                else None
            )
            latest_activity = self._latest_activity_from_cursor(activity_cursor)
            latest_event_type = (
                activity_event_type(latest_activity.type)
                if latest_activity is not None
                else None
            )
            summary = snapshot.model_dump(mode="json", exclude={"raw"})
            summary.update(
                {
                    "snapshot_key": row["snapshot_key"],
                    "scanned_at": row["updated_at"],
                    "discovered_event_emitted": bool(row["discovered_event_emitted"]),
                    "latest_event_checked": bool(
                        activity_cursor
                        and activity_cursor.get("latest_activity_checked") is True
                    ),
                    "latest_event": (
                        {
                            "event_type": latest_event_type,
                            "provider_event_type": latest_activity.type,
                            "provider_event_id": latest_activity.id,
                            "occurred_at": (
                                latest_activity.occurred_at.isoformat()
                                if latest_activity.occurred_at is not None
                                else None
                            ),
                        }
                        if latest_activity is not None and latest_event_type is not None
                        else None
                    ),
                }
            )
            results.append(summary)
        return results

    def get_change_request_detail(
        self,
        repository_id: str,
        number: int,
    ) -> dict[str, Any] | None:
        """按需返回 MR/PR 当前快照及其全部关联事件摘要。"""

        snapshots = self.list_snapshots(
            None,
            repository_id=repository_id,
            number=number,
        )
        if not snapshots:
            return None
        return {
            **snapshots[0],
            "events": self.list_events(
                None,
                repository_id=repository_id,
                number=number,
            ),
        }

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

    def pending_event_resources(
        self,
        max_attempts: int,
        limit: int = 100,
    ) -> list[tuple[str, int]]:
        """按最早事件顺序返回存在待处理工作的变更请求。"""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT repository_id, number, MIN(created_at) AS first_created_at
                FROM event_inbox
                WHERE status = 'pending'
                   OR (status = 'failed' AND attempts < ?)
                GROUP BY repository_id, number
                ORDER BY first_created_at ASC, repository_id ASC, number ASC
                LIMIT ?
                """,
                (max_attempts, limit),
            ).fetchall()
        return [(str(row["repository_id"]), int(row["number"])) for row in rows]

    def pending_events_for_resource(
        self,
        repository_id: str,
        number: int,
        *,
        max_attempts: int,
        limit: int = 100,
    ) -> list[ChangeEvent]:
        """读取单个 PR / MR 尚未处理的事件。"""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM event_inbox
                WHERE repository_id = ? AND number = ?
                  AND (
                      status = 'pending'
                      OR (status = 'failed' AND attempts < ?)
                  )
                ORDER BY created_at ASC, event_id ASC
                LIMIT ?
                """,
                (repository_id, number, max_attempts, limit),
            ).fetchall()
        return [ChangeEvent.model_validate_json(row["payload"]) for row in rows]

    def has_retryable_failed_events_for_resource(
        self,
        repository_id: str,
        number: int,
        *,
        max_attempts: int,
    ) -> bool:
        """判断指定变更请求是否仍有允许重试的失败事件。"""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM event_inbox
                WHERE repository_id = ? AND number = ?
                  AND status = 'failed' AND attempts < ?
                LIMIT 1
                """,
                (repository_id, number, max_attempts),
            ).fetchone()
        return row is not None

    def set_event_queue_reason(
        self,
        event_ids: Iterable[str],
        reason: str | None,
    ) -> None:
        """记录待处理事件尚未被领取的结构化原因。"""

        ids = tuple(dict.fromkeys(event_ids))
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        with self.connect() as connection:
            connection.execute(
                f"""
                UPDATE event_inbox
                SET queue_reason = ?, updated_at = ?
                WHERE event_id IN ({placeholders}) AND status = 'pending'
                """,
                (reason, time.time(), *ids),
            )

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
                    error = NULL, queue_reason = NULL, updated_at = ?
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
                    SET status = ?, error = NULL, queue_reason = NULL, updated_at = ?
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

        final_status = status or ("failed" if error else "completed")

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE event_inbox
                SET status = ?, error = ?, queue_reason = NULL, updated_at = ?
                WHERE event_id = ?
                """,
                (final_status, error, time.time(), event_id),
            )

    def release_event_after_service_shutdown(self, event_id: str) -> bool:
        """服务停止时退回事件，并抵消本轮领取增加的尝试次数。"""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE event_inbox
                SET status = 'pending',
                    attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                    error = '服务停止中断，事件已重新入队',
                    queue_reason = NULL, updated_at = ?
                WHERE event_id = ?
                  AND status IN ('processing', 'triggered', 'failed')
                """,
                (time.time(), event_id),
            )
        return cursor.rowcount == 1

    def cleanup_terminal_transient_event(self, event_id: str) -> bool:
        """固化运行上下文并删除已经成功结束的临时事件。"""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT payload FROM event_inbox
                WHERE event_id = ? AND event_type = ?
                  AND status IN ('completed', 'unmatched')
                """,
                (event_id, TARGET_COMMITS_CHANGED_EVENT),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            event = ChangeEvent.model_validate_json(row["payload"])
            snapshot = event.current_snapshot
            connection.execute(
                """
                UPDATE agent_runs
                SET repository_id = COALESCE(repository_id, ?),
                    change_request_number = COALESCE(change_request_number, ?),
                    change_request_title = COALESCE(change_request_title, ?),
                    change_request_url = COALESCE(change_request_url, ?)
                WHERE event_id = ?
                """,
                (
                    event.repository_id,
                    event.number,
                    snapshot.title,
                    snapshot.web_url,
                    event_id,
                ),
            )
            connection.execute(
                "DELETE FROM event_inbox WHERE event_id = ?",
                (event_id,),
            )
            connection.commit()
        return True

    def begin_preflight_run(
        self,
        *,
        proposed_run_id: str,
        idempotency_key: str,
        event_id: str,
        repository_id: str,
        number: int,
        head_sha: str,
        config_revision: str,
        max_attempts: int,
    ) -> PreflightReservation | None:
        """幂等创建 CI 运行；只有基础设施 error 可以复用记录重试。"""

        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT run_id, status, attempts
                FROM preflight_runs WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO preflight_runs (
                        run_id, idempotency_key, event_id, repository_id, number,
                        head_sha, config_revision, trigger_source, phase,
                        status, attempts, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'event', 'preparing',
                              'running', 1, ?)
                    """,
                    (
                        proposed_run_id,
                        idempotency_key,
                        event_id,
                        repository_id,
                        number,
                        head_sha,
                        config_revision,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO event_preflight_links (
                        event_id, run_id, reused, linked_at
                    )
                    SELECT ?, ?, 0, ?
                    WHERE EXISTS (
                        SELECT 1 FROM event_inbox WHERE event_id = ?
                    )
                    """,
                    (event_id, proposed_run_id, now, event_id),
                )
                connection.commit()
                return PreflightReservation(proposed_run_id, 1)

            if row["status"] != "error" or row["attempts"] >= max_attempts:
                connection.rollback()
                return None
            attempts = int(row["attempts"]) + 1
            connection.execute(
                """
                UPDATE preflight_runs
                SET status = 'running', attempts = ?, event_id = ?,
                    trigger_source = 'event', phase = 'preparing',
                    branch = NULL, cache_path = NULL, cancel_requested = 0,
                    failed_step = NULL, exit_code = NULL, output = '', error = NULL,
                    started_at = ?, finished_at = NULL
                WHERE run_id = ?
                """,
                (attempts, event_id, now, row["run_id"]),
            )
            connection.execute(
                """
                INSERT INTO event_preflight_links (
                    event_id, run_id, reused, linked_at
                )
                SELECT ?, ?, 0, ?
                WHERE EXISTS (
                    SELECT 1 FROM event_inbox WHERE event_id = ?
                )
                ON CONFLICT(event_id, run_id) DO UPDATE SET
                    reused = 0,
                    linked_at = excluded.linked_at
                """,
                (event_id, row["run_id"], now, event_id),
            )
            connection.commit()
            return PreflightReservation(str(row["run_id"]), attempts)

    def create_manual_preflight_run(
        self,
        *,
        run_id: str,
        repository_id: str,
        config_revision: str,
    ) -> None:
        """创建一次不绑定 MR / PR 事件的手动 CI 运行。"""

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO preflight_runs (
                    run_id, idempotency_key, event_id, repository_id, number,
                    head_sha, config_revision, trigger_source, phase,
                    status, attempts, started_at
                ) VALUES (?, ?, NULL, ?, NULL, '', ?, 'manual', 'queued',
                          'running', 1, ?)
                """,
                (
                    run_id,
                    f"manual:{run_id}",
                    repository_id,
                    config_revision,
                    time.time(),
                ),
            )

    def set_preflight_phase(
        self,
        run_id: str,
        phase: str,
        *,
        branch: str | None = None,
        head_sha: str | None = None,
        cache_path: str | None = None,
    ) -> None:
        """更新 CI 当前阶段，并按需补充实际分支、提交和缓存目录。"""

        assignments = ["phase = ?"]
        parameters: list[Any] = [phase]
        for column, value in (
            ("branch", branch),
            ("head_sha", head_sha),
            ("cache_path", cache_path),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                parameters.append(value)
        parameters.append(run_id)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE preflight_runs SET {', '.join(assignments)} "
                "WHERE run_id = ?",
                parameters,
            )

    def preflight_cancel_requested(self, run_id: str) -> bool:
        """返回手动 CI 是否已收到取消请求。"""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM preflight_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return row is not None and bool(row["cancel_requested"])

    def request_cancel_preflight(self, run_id: str) -> bool:
        """为仍在运行的手动 CI 设置取消标记。"""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE preflight_runs
                SET cancel_requested = 1, phase = 'cancelling'
                WHERE run_id = ? AND trigger_source = 'manual'
                  AND status = 'running'
                """,
                (run_id,),
            )
        return cursor.rowcount > 0

    def link_events_to_preflight(
        self,
        event_ids: Iterable[str],
        run_id: str,
        *,
        reused: bool,
    ) -> None:
        """把一次新执行或复用的 CI 结果关联到当前匹配事件。"""

        ids = tuple(dict.fromkeys(event_ids))
        if not ids:
            return
        now = time.time()
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO event_preflight_links (
                    event_id, run_id, reused, linked_at
                )
                SELECT ?, ?, ?, ?
                WHERE EXISTS (
                    SELECT 1 FROM event_inbox WHERE event_id = ?
                ) AND EXISTS (
                    SELECT 1 FROM preflight_runs WHERE run_id = ?
                )
                ON CONFLICT(event_id, run_id) DO UPDATE SET
                    reused = excluded.reused,
                    linked_at = excluded.linked_at
                """,
                [
                    (event_id, run_id, int(reused), now, event_id, run_id)
                    for event_id in ids
                ],
            )

    def initialize_preflight_steps(
        self,
        run_id: str,
        steps: Iterable[dict[str, Any]],
    ) -> None:
        """为当前 Preflight 尝试重建不可变的命令步骤快照。"""

        records = tuple(steps)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM preflight_step_runs WHERE run_id = ?",
                (run_id,),
            )
            connection.executemany(
                """
                INSERT INTO preflight_step_runs (
                    run_id, step_index, name, command, status, timeout_seconds
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                [
                    (
                        run_id,
                        index,
                        str(step["name"]),
                        json.dumps(list(step["command"]), ensure_ascii=False),
                        step.get("timeout_seconds"),
                    )
                    for index, step in enumerate(records)
                ],
            )

    def update_preflight_step(
        self,
        run_id: str,
        step_index: int,
        *,
        status: str,
        timeout_seconds: float | None = None,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> None:
        """更新单个 CI 步骤的实时状态与最终结果。"""

        now = time.time()
        terminal = status in {
            "success",
            "failure",
            "timed_out",
            "error",
            "cancelled",
            "skipped",
        }
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE preflight_step_runs
                SET status = ?,
                    timeout_seconds = COALESCE(?, timeout_seconds),
                    started_at = CASE
                        WHEN ? = 'running' THEN COALESCE(started_at, ?)
                        ELSE started_at
                    END,
                    finished_at = CASE WHEN ? THEN ? ELSE NULL END,
                    exit_code = ?, error = ?
                WHERE run_id = ? AND step_index = ?
                """,
                (
                    status,
                    timeout_seconds,
                    status,
                    now,
                    int(terminal),
                    now,
                    exit_code,
                    error,
                    run_id,
                    step_index,
                ),
            )

    def load_preflight_result(self, idempotency_key: str) -> PreflightResult | None:
        """按幂等键读取当前 CI 结果。"""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, repository_id, number, head_sha, status,
                       failed_step, exit_code, output, error, status_published
                FROM preflight_runs WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return PreflightResult(
            run_id=str(row["run_id"]),
            repository_id=str(row["repository_id"]),
            number=None if row["number"] is None else int(row["number"]),
            head_sha=str(row["head_sha"]),
            status=str(row["status"]),
            failed_step=row["failed_step"],
            exit_code=row["exit_code"],
            output=str(row["output"] or ""),
            error=row["error"],
            status_published=bool(row["status_published"]),
        )

    def finish_preflight_run(self, result: PreflightResult) -> None:
        """保存一次 CI 运行的终态与有界输出。"""

        finished_at = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE preflight_runs
                SET status = ?, failed_step = ?, exit_code = ?, output = ?,
                    error = ?, status_published = ?, phase = 'finished',
                    finished_at = ?
                WHERE run_id = ?
                """,
                (
                    result.status,
                    result.failed_step,
                    result.exit_code,
                    result.output,
                    result.error,
                    int(result.status_published),
                    finished_at,
                    result.run_id,
                ),
            )
            connection.execute(
                """
                UPDATE preflight_step_runs
                SET status = CASE
                        WHEN ? = 'cancelled' AND status = 'running'
                        THEN 'cancelled'
                        WHEN status = 'running' THEN 'error'
                        ELSE 'skipped'
                    END,
                    error = COALESCE(
                        error,
                        CASE
                            WHEN status = 'running'
                            THEN CASE
                                WHEN ? = 'cancelled' THEN '用户取消了手动 CI'
                                ELSE 'Preflight 已结束，但步骤未正常收尾'
                            END
                            ELSE '前序步骤结束后未执行'
                        END
                    ),
                    finished_at = ?
                WHERE run_id = ? AND status IN ('pending', 'running')
                """,
                (result.status, result.status, finished_at, result.run_id),
            )

    def mark_preflight_status_published(self, run_id: str) -> None:
        """记录终态 Commit Status 已成功发布，重试时无需重新执行 CI。"""

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE preflight_runs
                SET status_published = 1
                WHERE run_id = ?
                """,
                (run_id,),
            )

    def get_preflight_failure_comment(
        self,
        repository_id: str,
        number: int,
    ) -> dict[str, object] | None:
        """读取一个 MR/PR 当前由服务维护的失败评论映射。"""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT repository_id, number, status_context, remote_comment_id,
                       head_sha, content_hash, updated_at
                FROM preflight_failure_comments
                WHERE repository_id = ? AND number = ?
                """,
                (repository_id, number),
            ).fetchone()
        return dict(row) if row is not None else None

    def save_preflight_failure_comment(
        self,
        *,
        repository_id: str,
        number: int,
        status_context: str,
        remote_comment_id: str,
        head_sha: str,
        content_hash: str,
    ) -> None:
        """保存或更新失败评论映射，确保后续失败复用同一条评论。"""

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO preflight_failure_comments (
                    repository_id, number, status_context, remote_comment_id,
                    head_sha, content_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository_id, number) DO UPDATE SET
                    status_context = excluded.status_context,
                    remote_comment_id = excluded.remote_comment_id,
                    head_sha = excluded.head_sha,
                    content_hash = excluded.content_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    repository_id,
                    number,
                    status_context,
                    remote_comment_id,
                    head_sha,
                    content_hash,
                    time.time(),
                ),
            )

    def delete_preflight_failure_comment(
        self,
        repository_id: str,
        number: int,
    ) -> None:
        """删除本地失败评论映射；远端删除应由调用方先完成。"""

        with self.connect() as connection:
            connection.execute(
                """
                DELETE FROM preflight_failure_comments
                WHERE repository_id = ? AND number = ?
                """,
                (repository_id, number),
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
                SELECT run_id, root_run_id, parent_run_id, status, attempts,
                       cancel_source
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

            service_interrupted = (
                row["status"] == "cancelled"
                and row["cancel_source"] == CANCEL_SOURCE_SERVICE_SHUTDOWN
            )
            retryable_failure = (
                row["status"] in {"failed", "timed_out"}
                and row["attempts"] < max_attempts
            )
            if not service_interrupted and not retryable_failure:
                connection.rollback()
                return None
            attempts = (
                int(row["attempts"])
                if service_interrupted
                else int(row["attempts"]) + 1
            )
            connection.execute(
                """
                UPDATE agent_runs
                SET status = 'queued', attempts = ?, prompt = ?, environment = ?,
                    config_revision = ?, error = NULL,
                    final_message = NULL, events = NULL, usage = NULL,
                    workspace_path = NULL, workspace_status = NULL,
                    workspace_reason = NULL, cancel_requested = 0,
                    cancel_source = NULL, concurrency_acquired = 0,
                    queue_reason = NULL,
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

    def try_acquire_agent_run_capacity(
        self,
        run_id: str,
        *,
        global_limit: int,
        runtime_limit: int,
        agent_limit: int | None,
        acquire_global: bool,
    ) -> tuple[bool, str | None]:
        """原子申请根任务总额度和同名 Agent 额度。"""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT agent_name, status, cancel_requested,
                       concurrency_acquired
                FROM agent_runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] not in {"queued", "preparing", "running"}
                or row["cancel_requested"]
            ):
                connection.rollback()
                return False, None
            if row["concurrency_acquired"]:
                connection.rollback()
                return True, None

            reason: str | None = None
            if acquire_global:
                global_count = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM agent_runs
                    WHERE concurrency_acquired = 1
                      AND parent_run_id IS NULL
                      AND status IN ('queued', 'preparing', 'running')
                    """
                ).fetchone()
                active_roots = int(global_count["count"])
                if active_roots >= global_limit:
                    reason = "global_concurrency"
                elif active_roots >= runtime_limit:
                    reason = "runtime_concurrency"
            if reason is None and agent_limit is not None:
                agent_count = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM agent_runs
                    WHERE concurrency_acquired = 1
                      AND agent_name = ?
                      AND status IN ('queued', 'preparing', 'running')
                    """,
                    (row["agent_name"],),
                ).fetchone()
                if int(agent_count["count"]) >= agent_limit:
                    reason = "agent_concurrency"

            if reason is not None:
                connection.execute(
                    """
                    UPDATE agent_runs SET queue_reason = ?
                    WHERE run_id = ? AND status = 'queued'
                    """,
                    (reason, run_id),
                )
                connection.commit()
                return False, reason

            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET concurrency_acquired = 1, queue_reason = NULL
                WHERE run_id = ? AND status = 'queued'
                  AND cancel_requested = 0
                """,
                (run_id,),
            )
            connection.commit()
        return cursor.rowcount == 1, None

    def set_agent_run_queue_reason(
        self,
        run_id: str,
        reason: str | None,
    ) -> None:
        """更新仍在排队运行的结构化等待原因。"""

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE agent_runs SET queue_reason = ?
                WHERE run_id = ? AND status = 'queued'
                """,
                (reason, run_id),
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
                SET status = 'running', queue_reason = NULL
                WHERE run_id = ? AND status IN ('queued', 'preparing')
                    AND cancel_requested = 0
                """,
                (run_id,),
            )
        return cursor.rowcount == 1

    def mark_agent_run_preparing(self, run_id: str) -> bool:
        """取得仓库管理锁后把 Agent 从排队切换为准备工作区。"""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET status = 'preparing', queue_reason = NULL
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

    def agent_run_cancel_source(self, run_id: str) -> str | None:
        """返回某次运行最近一次取消请求的来源。"""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_source FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return None if row is None or row["cancel_source"] is None else str(
            row["cancel_source"]
        )

    def agent_run_cancel_source_by_idempotency(
        self,
        idempotency_key: str,
    ) -> str | None:
        """按幂等键返回运行最近一次取消请求的来源。"""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_source FROM agent_runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return None if row is None or row["cancel_source"] is None else str(
            row["cancel_source"]
        )

    def request_cancel_run(
        self,
        run_id: str,
        *,
        source: str = CANCEL_SOURCE_ADMINISTRATOR,
    ) -> list[str] | None:
        """取消指定运行及全部后代，并立即结束仍在排队的运行。"""

        if source not in {
            CANCEL_SOURCE_ADMINISTRATOR,
            CANCEL_SOURCE_SERVICE_SHUTDOWN,
        }:
            raise ValueError(f"不支持的取消来源：{source}")
        queued_error = (
            "服务正在停止，运行已取消"
            if source == CANCEL_SOURCE_SERVICE_SHUTDOWN
            else "运行已由管理员取消"
        )
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
                if row["status"] in {"queued", "preparing", "running"}
            ]
            if active_ids:
                placeholders = ", ".join("?" for _ in active_ids)
                connection.execute(
                    f"""
                    UPDATE agent_runs
                    SET cancel_requested = 1, cancel_source = ?,
                        status = CASE
                            WHEN status = 'queued' THEN 'cancelled'
                            ELSE status
                        END,
                        error = CASE
                            WHEN status = 'queued' THEN ?
                            ELSE error
                        END,
                        finished_at = CASE
                            WHEN status = 'queued' THEN ?
                            ELSE finished_at
                        END,
                        concurrency_acquired = CASE
                            WHEN status = 'queued' THEN 0
                            ELSE concurrency_acquired
                        END,
                        queue_reason = CASE
                            WHEN status = 'queued' THEN NULL
                            ELSE queue_reason
                        END
                    WHERE run_id IN ({placeholders})
                    """,
                    (source, queued_error, now, *active_ids),
                )
            connection.commit()
        return active_ids

    def request_cancel_active_runs(self) -> list[str]:
        """为服务内全部活动运行请求取消，并立即结束排队任务。"""

        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT run_id FROM agent_runs
                WHERE status IN ('queued', 'preparing', 'running')
                  AND cancel_requested = 0
                ORDER BY started_at ASC
                """
            ).fetchall()
            active_ids = [str(row["run_id"]) for row in rows]
            if active_ids:
                placeholders = ", ".join("?" for _ in active_ids)
                connection.execute(
                    f"""
                    UPDATE agent_runs
                    SET cancel_requested = 1, cancel_source = ?,
                        status = CASE
                            WHEN status = 'queued' THEN 'cancelled'
                            ELSE status
                        END,
                        error = CASE
                            WHEN status = 'queued' THEN '服务正在停止，运行已取消'
                            ELSE error
                        END,
                        finished_at = CASE
                            WHEN status = 'queued' THEN ?
                            ELSE finished_at
                        END,
                        concurrency_acquired = CASE
                            WHEN status = 'queued' THEN 0
                            ELSE concurrency_acquired
                        END,
                        queue_reason = CASE
                            WHEN status = 'queued' THEN NULL
                            ELSE queue_reason
                        END
                    WHERE run_id IN ({placeholders})
                    """,
                    (CANCEL_SOURCE_SERVICE_SHUTDOWN, now, *active_ids),
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
                    events = ?, error = ?, concurrency_acquired = 0,
                    queue_reason = NULL, finished_at = ?
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

    def append_preflight_log(
        self,
        run_id: str,
        *,
        stream: str,
        event_type: str,
        payload: str | dict[str, Any] | list[Any],
    ) -> int:
        """追加一条可供 CI 详情页按游标实时读取的日志。"""

        encoded = (
            payload
            if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False)
        )
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO preflight_logs (
                    run_id, created_at, stream, event_type, payload
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, time.time(), stream, event_type, encoded),
            )
        return int(cursor.lastrowid)

    def list_preflight_logs(
        self,
        run_id: str,
        *,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """返回某次 CI 在指定游标之后产生的实时日志。"""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, created_at, stream, event_type, payload
                FROM preflight_logs
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
                SELECT agent_runs.*,
                       event_inbox.repository_id AS event_repository_id,
                       event_inbox.number AS event_change_request_number,
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
                       queue_reason, workspace_path, workspace_status,
                       workspace_reason
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

        event_repository_id = record.pop("event_repository_id", None)
        event_change_request_number = record.pop(
            "event_change_request_number",
            None,
        )
        if not record.get("repository_id") and event_repository_id:
            record["repository_id"] = event_repository_id
        if (
            record.get("change_request_number") is None
            and event_change_request_number is not None
        ):
            record["change_request_number"] = event_change_request_number
        event_payload = record.pop("event_payload", None)
        if not event_payload:
            return record
        try:
            event = json.loads(event_payload)
        except (TypeError, json.JSONDecodeError):
            return record
        snapshot = event.get("current") or event.get("new") or {}
        if not record.get("change_request_title"):
            record["change_request_title"] = snapshot.get("title")
        if not record.get("change_request_url"):
            record["change_request_url"] = snapshot.get("web_url")
        return record

    def list_runs(
        self,
        limit: int | None = 20,
        *,
        status: str | None = None,
        statuses: Sequence[str] | None = None,
        agent_name: str | None = None,
        repository_id: str | None = None,
        number: int | None = None,
    ) -> list[dict[str, Any]]:
        """按可选条件返回最近 Agent 运行摘要。"""

        conditions: list[str] = []
        parameters: list[Any] = []
        if statuses is not None:
            if not statuses:
                conditions.append("1 = 0")
            else:
                placeholders = ", ".join("?" for _ in statuses)
                conditions.append(f"agent_runs.status IN ({placeholders})")
                parameters.extend(statuses)
        elif status:
            conditions.append("agent_runs.status = ?")
            parameters.append(status)
        if agent_name:
            conditions.append("agent_runs.agent_name = ?")
            parameters.append(agent_name)
        if repository_id:
            conditions.append(
                "(agent_runs.repository_id = ? OR event_inbox.repository_id = ? "
                "OR (agent_runs.repository_id IS NULL "
                "AND event_inbox.repository_id IS NULL "
                "AND agent_runs.resource_key LIKE ?))"
            )
            parameters.extend(
                (repository_id, repository_id, f"%:{repository_id}:%")
            )
        if number is not None:
            conditions.append(
                "(agent_runs.change_request_number = ? "
                "OR event_inbox.number = ?)"
            )
            parameters.extend((number, number))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            parameters.append(limit)

        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT agent_runs.run_id, agent_runs.root_run_id,
                       agent_runs.parent_run_id, agent_runs.event_id,
                       agent_runs.rule_name, agent_runs.agent_name,
                       agent_runs.resource_key, agent_runs.status,
                       agent_runs.attempts, agent_runs.error,
                       agent_runs.queue_reason,
                       agent_runs.cancel_requested, agent_runs.cancel_source,
                       agent_runs.workspace_path, agent_runs.workspace_status,
                       agent_runs.workspace_reason, agent_runs.started_at,
                       agent_runs.finished_at, agent_runs.repository_id,
                       agent_runs.change_request_number,
                       agent_runs.change_request_title,
                       agent_runs.change_request_url,
                       event_inbox.repository_id AS event_repository_id,
                       event_inbox.number AS event_change_request_number,
                       event_inbox.payload AS event_payload
                FROM agent_runs
                LEFT JOIN event_inbox ON event_inbox.event_id = agent_runs.event_id
                {where}
                ORDER BY agent_runs.started_at DESC, agent_runs.run_id DESC
                {limit_clause}
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
        limit: int | None = 50,
        *,
        status: str | None = None,
        repository_id: str | None = None,
        number: int | None = None,
        event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """返回最近 MR/PR 语义事件。"""

        conditions: list[str] = []
        parameters: list[Any] = []
        if status:
            conditions.append("event_inbox.status = ?")
            parameters.append(status)
        if repository_id:
            conditions.append("event_inbox.repository_id = ?")
            parameters.append(repository_id)
        if number is not None:
            conditions.append("event_inbox.number = ?")
            parameters.append(number)
        if event_id:
            conditions.append("event_inbox.event_id = ?")
            parameters.append(event_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT event_inbox.event_id, event_inbox.event_type,
                       event_inbox.repository_id, event_inbox.number,
                       event_inbox.status, event_inbox.attempts,
                       event_inbox.error, event_inbox.queue_reason,
                       event_inbox.created_at,
                       event_inbox.updated_at,
                       json_extract(event_inbox.payload, '$.occurred_at')
                           AS occurred_at,
                       COALESCE(
                           json_extract(event_inbox.payload, '$.origin'),
                           'scanner'
                       ) AS origin,
                       json_extract(event_inbox.payload, '$.source_activity_id')
                           AS source_activity_id,
                       json_extract(event_inbox.payload, '$.source_activity_type')
                           AS source_activity_type,
                       json_extract(event_inbox.payload, '$.source_occurred_at')
                           AS source_occurred_at,
                       preflight.status AS preflight_status,
                       preflight.run_id AS preflight_run_id,
                       preflight.exit_code AS preflight_exit_code,
                       preflight.failed_step AS preflight_failed_step,
                       preflight.error AS preflight_error,
                       preflight.status_published AS preflight_status_published,
                       preflight_link.reused AS preflight_reused,
                       COALESCE(dispatch_stats.trigger_count, 0) AS trigger_count,
                       COALESCE(dispatch_stats.sub_agent_count, 0)
                           AS sub_agent_count,
                       COALESCE(dispatch_stats.agent_queued_count, 0)
                           AS agent_queued_count,
                       COALESCE(dispatch_stats.agent_preparing_count, 0)
                           AS agent_preparing_count,
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
                           COUNT(DISTINCT dispatch.idempotency_key)
                               AS trigger_count,
                           SUM(
                               CASE
                                   WHEN family.parent_run_id IS NOT NULL
                                   THEN 1 ELSE 0
                               END
                           ) AS sub_agent_count,
                           SUM(
                               CASE
                                   WHEN family.run_id IS NULL
                                     OR family.status = 'queued'
                                   THEN 1 ELSE 0
                               END
                           ) AS agent_queued_count,
                           SUM(CASE WHEN family.status = 'preparing' THEN 1 ELSE 0 END)
                               AS agent_preparing_count,
                           SUM(CASE WHEN family.status = 'running' THEN 1 ELSE 0 END)
                               AS agent_running_count,
                           SUM(CASE WHEN family.status = 'completed' THEN 1 ELSE 0 END)
                               AS agent_completed_count,
                           SUM(CASE WHEN family.status = 'failed' THEN 1 ELSE 0 END)
                               AS agent_failed_count,
                           SUM(CASE WHEN family.status = 'timed_out' THEN 1 ELSE 0 END)
                               AS agent_timed_out_count,
                           SUM(CASE WHEN family.status = 'cancelled' THEN 1 ELSE 0 END)
                               AS agent_cancelled_count
                    FROM event_agent_dispatches AS dispatch
                    LEFT JOIN agent_runs AS root_run
                        ON root_run.idempotency_key = dispatch.idempotency_key
                    LEFT JOIN agent_runs AS family
                        ON family.root_run_id = root_run.run_id
                    GROUP BY dispatch.event_id
                ) AS dispatch_stats
                    ON dispatch_stats.event_id = event_inbox.event_id
                LEFT JOIN event_preflight_links AS preflight_link
                    ON preflight_link.id = (
                        SELECT candidate.id
                        FROM event_preflight_links AS candidate
                        WHERE candidate.event_id = event_inbox.event_id
                        ORDER BY candidate.linked_at DESC, candidate.id DESC
                        LIMIT 1
                    )
                LEFT JOIN preflight_runs AS preflight
                    ON preflight.run_id = preflight_link.run_id
                {where}
                ORDER BY julianday(
                             json_extract(event_inbox.payload, '$.occurred_at')
                         ) DESC,
                         event_inbox.created_at DESC,
                         event_inbox.event_id DESC
                {limit_clause}
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_event_detail(self, event_id: str) -> dict[str, Any] | None:
        """按需返回单个事件、Agent 调度和全部 CI 摘要。"""

        records = self.list_events(None, event_id=event_id)
        if not records:
            return None
        with self.connect() as connection:
            dispatch_rows = connection.execute(
                """
                SELECT dispatch.idempotency_key, dispatch.rule_name,
                       dispatch.agent_name, dispatch.created_at,
                       run.run_id, run.root_run_id, run.parent_run_id,
                       run.status AS run_status,
                       run.error AS run_error, run.started_at,
                       run.finished_at
                FROM event_agent_dispatches AS dispatch
                LEFT JOIN agent_runs AS run
                    ON run.idempotency_key = dispatch.idempotency_key
                WHERE dispatch.event_id = ?
                ORDER BY dispatch.created_at, dispatch.rule_name,
                         dispatch.agent_name
                """,
                (event_id,),
            ).fetchall()
            agent_run_rows = connection.execute(
                """
                SELECT family.run_id, family.root_run_id,
                       family.parent_run_id, family.idempotency_key,
                       family.rule_name, family.agent_name,
                       family.status AS run_status,
                       family.error AS run_error, family.started_at,
                       family.finished_at
                FROM event_agent_dispatches AS dispatch
                JOIN agent_runs AS root_run
                    ON root_run.idempotency_key = dispatch.idempotency_key
                JOIN agent_runs AS family
                    ON family.root_run_id = root_run.run_id
                WHERE dispatch.event_id = ?
                ORDER BY family.started_at, family.run_id
                """,
                (event_id,),
            ).fetchall()
            preflight_rows = connection.execute(
                """
                SELECT preflight.run_id, preflight.repository_id,
                       preflight.number, preflight.head_sha,
                       preflight.config_revision, preflight.status,
                       preflight.attempts, preflight.failed_step,
                       preflight.exit_code,
                       preflight.error, preflight.status_published,
                       preflight.started_at, preflight.finished_at,
                       link.reused, link.linked_at
                FROM event_preflight_links AS link
                JOIN preflight_runs AS preflight
                    ON preflight.run_id = link.run_id
                WHERE link.event_id = ?
                ORDER BY link.linked_at DESC, link.id DESC
                """,
                (event_id,),
            ).fetchall()
        preflights = [dict(row) for row in preflight_rows]
        return {
            **records[0],
            "dispatches": [dict(row) for row in dispatch_rows],
            "agent_runs": [dict(row) for row in agent_run_rows],
            "preflights": preflights,
            # 保留最新单条字段，兼容只识别旧事件详情结构的客户端。
            "preflight": preflights[0] if preflights else None,
        }

    def list_preflight_runs(
        self,
        limit: int | None = 100,
        *,
        status: str | None = None,
        statuses: Sequence[str] | None = None,
        repository_id: str | None = None,
        number: int | None = None,
    ) -> list[dict[str, Any]]:
        """按可选条件返回最近本地 Preflight / CI 运行摘要。"""

        conditions: list[str] = []
        parameters: list[Any] = []
        if statuses is not None:
            if not statuses:
                conditions.append("1 = 0")
            else:
                placeholders = ", ".join("?" for _ in statuses)
                conditions.append(f"preflight.status IN ({placeholders})")
                parameters.extend(statuses)
        elif status:
            conditions.append("preflight.status = ?")
            parameters.append(status)
        if repository_id:
            conditions.append("preflight.repository_id = ?")
            parameters.append(repository_id)
        if number is not None:
            conditions.append("preflight.number = ?")
            parameters.append(number)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT preflight.run_id, preflight.event_id,
                       preflight.repository_id, preflight.number,
                       preflight.head_sha, preflight.config_revision,
                       preflight.trigger_source, preflight.branch,
                       preflight.phase, preflight.cache_path,
                       preflight.cancel_requested,
                       preflight.status, preflight.attempts,
                       preflight.failed_step, preflight.exit_code,
                       preflight.error, preflight.status_published,
                       preflight.started_at, preflight.finished_at,
                       event.event_type,
                       json_extract(snapshot.payload, '$.title')
                           AS change_request_title,
                       json_extract(snapshot.payload, '$.web_url')
                           AS change_request_url,
                       COALESCE(link_stats.linked_event_count, 0)
                           AS linked_event_count,
                       COALESCE(link_stats.reused_event_count, 0)
                           AS reused_event_count
                FROM preflight_runs AS preflight
                LEFT JOIN event_inbox AS event
                    ON event.event_id = preflight.event_id
                LEFT JOIN snapshots AS snapshot
                    ON snapshot.snapshot_key = (
                        preflight.repository_id || ':' || preflight.number
                    )
                LEFT JOIN (
                    SELECT run_id, COUNT(*) AS linked_event_count,
                           SUM(CASE WHEN reused = 1 THEN 1 ELSE 0 END)
                               AS reused_event_count
                    FROM event_preflight_links
                    GROUP BY run_id
                ) AS link_stats ON link_stats.run_id = preflight.run_id
                {where}
                ORDER BY preflight.started_at DESC, preflight.run_id DESC
                {limit_clause}
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_preflight_run(self, run_id: str) -> dict[str, Any] | None:
        """按需返回单次本地 Preflight / CI 的完整结果与事件关联。"""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT preflight.run_id, preflight.event_id,
                       preflight.repository_id, preflight.number,
                       preflight.head_sha, preflight.config_revision,
                       preflight.trigger_source, preflight.branch,
                       preflight.phase, preflight.cache_path,
                       preflight.cancel_requested,
                       preflight.status, preflight.attempts,
                       preflight.failed_step, preflight.exit_code,
                       preflight.output, preflight.error,
                       preflight.status_published, preflight.started_at,
                       preflight.finished_at,
                       event.event_type,
                       json_extract(snapshot.payload, '$.title')
                           AS change_request_title,
                       json_extract(snapshot.payload, '$.web_url')
                           AS change_request_url
                FROM preflight_runs AS preflight
                LEFT JOIN event_inbox AS event
                    ON event.event_id = preflight.event_id
                LEFT JOIN snapshots AS snapshot
                    ON snapshot.snapshot_key = (
                        preflight.repository_id || ':' || preflight.number
                    )
                WHERE preflight.run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            event_rows = connection.execute(
                """
                SELECT event.event_id, event.event_type, event.status,
                       json_extract(event.payload, '$.occurred_at')
                           AS occurred_at,
                       link.reused, link.linked_at
                FROM event_preflight_links AS link
                JOIN event_inbox AS event ON event.event_id = link.event_id
                WHERE link.run_id = ?
                ORDER BY link.linked_at DESC, link.id DESC
                """,
                (run_id,),
            ).fetchall()
            step_rows = connection.execute(
                """
                SELECT step_index, name, command, status, timeout_seconds,
                       started_at, finished_at, exit_code, error
                FROM preflight_step_runs
                WHERE run_id = ?
                ORDER BY step_index
                """,
                (run_id,),
            ).fetchall()
        steps: list[dict[str, Any]] = []
        for step_row in step_rows:
            step = dict(step_row)
            try:
                command = json.loads(str(step["command"]))
            except (TypeError, ValueError):
                command = []
            step["command"] = command if isinstance(command, list) else []
            steps.append(step)
        return {
            **dict(row),
            "linked_events": [dict(event_row) for event_row in event_rows],
            "steps": steps,
        }

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
        """删除超过保留期的 Agent 与 CI 实时日志。"""

        with self.connect() as connection:
            run_cursor = connection.execute(
                "DELETE FROM run_logs WHERE created_at < ?",
                (older_than,),
            )
            preflight_cursor = connection.execute(
                "DELETE FROM preflight_logs WHERE created_at < ?",
                (older_than,),
            )
        return run_cursor.rowcount + preflight_cursor.rowcount
