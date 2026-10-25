"""SQLite 底层故障分阶段诊断及原始异常保护测试。"""

from __future__ import annotations

import errno
import json
import logging
import sqlite3
from unittest.mock import MagicMock, Mock

import pytest

from teamwork_review_agents import sqlite_diagnostics
from teamwork_review_agents.state import StateStore


def database_error(message: str = "unable to open database file") -> sqlite3.OperationalError:
    """构造包含扩展错误码的数据库故障。"""

    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN_FULLPATH
    error.sqlite_errorname = "SQLITE_CANTOPEN_FULLPATH"
    return error


def diagnostics_records(caplog) -> list[dict]:
    """只解析诊断模块的结构化日志。"""

    return [
        json.loads(record.args[0])
        for record in caplog.records
        if record.name == sqlite_diagnostics.__name__
    ]


def fake_connection(monkeypatch) -> MagicMock:
    """用可注入提交、回滚和关闭错误的连接替代真实数据库。"""

    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.__exit__.return_value = False
    monkeypatch.setattr("teamwork_review_agents.state.sqlite3.connect", Mock(return_value=connection))
    return connection


def test_open_failure_reports_real_error_and_resource_context(tmp_path, caplog) -> None:
    """父目录缺失时记录原始错误、资源状态和最近可检查的磁盘位置。"""

    store = StateStore(tmp_path / "missing" / "state.db")
    with pytest.raises(sqlite3.OperationalError) as caught:
        with store.connect():
            pytest.fail("打开失败后不应进入操作体")

    details, = diagnostics_records(caplog)
    assert details["phase"] == "open"
    assert details["sqlite_errorcode"] == caught.value.sqlite_errorcode
    assert details["sqlite_primary_errorcode"] == sqlite3.SQLITE_CANTOPEN
    assert details["os_errno"] is None
    assert details["database_path"] == str(store.path)
    assert details["paths"]["directory"]["errno"] == errno.ENOENT
    assert details["disk"]["sample_path"] == str(tmp_path)
    assert details["disk"]["free_bytes"] >= 0
    assert details["process"]["pid"] > 0
    assert "fd_limits" in details["process"]
    assert not store.path.parent.exists()
    assert caplog.records[-1].exc_info[1] is caught.value
    assert "Traceback" in caplog.text


@pytest.mark.parametrize(
    ("statement", "phase"),
    [
        ("PRAGMA journal_mode=WAL", "enable_wal"),
        ("PRAGMA foreign_keys=ON", "enable_foreign_keys"),
        ("PRAGMA busy_timeout=30000", "set_busy_timeout"),
    ],
)
def test_connection_initialization_failure_keeps_phase_and_closes_connection(
    tmp_path, monkeypatch, caplog, statement, phase,
) -> None:
    """连接已打开但初始化失败时，记录准确阶段并关闭连接。"""

    connection = fake_connection(monkeypatch)
    error = database_error()

    def execute(sql: str) -> None:
        """仅在指定初始化步骤注入故障。"""

        if sql == statement:
            raise error

    connection.execute.side_effect = execute
    with pytest.raises(sqlite3.OperationalError) as caught:
        with StateStore(tmp_path / "state.db").connect():
            pytest.fail("初始化失败后不应进入操作体")

    assert caught.value is error
    details, = diagnostics_records(caplog)
    assert details["phase"] == phase
    assert details["sqlite_errorcode"] == sqlite3.SQLITE_CANTOPEN_FULLPATH
    assert details["sqlite_primary_errorcode"] == sqlite3.SQLITE_CANTOPEN
    assert details["sqlite_errorname"] == "SQLITE_CANTOPEN_FULLPATH"
    connection.close.assert_called_once()


def test_operation_failure_rolls_back_without_logging_parameters(tmp_path, caplog) -> None:
    """执行失败仍按原事务语义回滚关闭，日志不输出参数值。"""

    store = StateStore(tmp_path / "state.db")
    with store.connect() as connection:
        connection.execute("CREATE TABLE example (value TEXT)")
    secret = "不得出现在日志中的参数值"
    with pytest.raises(sqlite3.OperationalError) as caught:
        with store.connect() as connection:
            connection.execute("INSERT INTO example VALUES (?)", (secret,))
            connection.execute("INSERT INTO missing VALUES (?)", (secret,))

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM example").fetchone()[0] == 0
    details, = diagnostics_records(caplog)
    assert details["phase"] == "operation"
    assert details["paths"]["database"]["hard_links"] >= 1
    assert "mode" in details["paths"]["directory"]
    assert caplog.records[-1].exc_info[1] is caught.value
    assert secret not in caplog.text


@pytest.mark.parametrize("phase", ["commit", "close"])
def test_finalize_failure_is_reported_and_propagated(tmp_path, monkeypatch, caplog, phase) -> None:
    """操作体成功后，提交或关闭失败不能被误判为成功。"""

    connection = fake_connection(monkeypatch)
    error = database_error()
    if phase == "commit":
        connection.__exit__.side_effect = error
    else:
        connection.close.side_effect = error
    with pytest.raises(sqlite3.OperationalError) as caught:
        with StateStore(tmp_path / "state.db").connect():
            pass

    assert caught.value is error
    details, = diagnostics_records(caplog)
    assert details["phase"] == phase
    assert details["secondary_failure"] is False
    connection.close.assert_called_once()


def test_rollback_and_close_errors_do_not_hide_original_failure(tmp_path, monkeypatch, caplog) -> None:
    """同时发生回滚、关闭错误时，调用方仍收到最初的操作异常。"""

    connection = fake_connection(monkeypatch)
    original = database_error("original operation failure")
    rollback = database_error("rollback failure")
    connection.__exit__.side_effect = rollback
    connection.close.side_effect = database_error("close failure")
    with pytest.raises(sqlite3.OperationalError) as caught:
        with StateStore(tmp_path / "state.db").connect():
            raise original

    assert caught.value is original
    assert caught.value.__cause__ is rollback
    records = diagnostics_records(caplog)
    assert [(item["phase"], item["secondary_failure"]) for item in records] == [
        ("operation", False), ("rollback", True), ("close", True),
    ]


def test_probe_failures_are_isolated_and_keep_actual_errno(tmp_path, monkeypatch) -> None:
    """磁盘和句柄探针不可用时仍保留路径信息及实际探针错误。"""

    path = tmp_path / "state.db"
    path.touch()
    monkeypatch.setattr(sqlite_diagnostics.shutil, "disk_usage", Mock(side_effect=PermissionError(errno.EACCES, "denied")))
    monkeypatch.setattr(sqlite_diagnostics, "_descriptor_limits", Mock(side_effect=ImportError("unsupported platform")))
    monkeypatch.setattr(sqlite_diagnostics.psutil, "Process", Mock(side_effect=PermissionError(errno.EACCES, "denied")))
    details = sqlite_diagnostics.collect_sqlite_diagnostics(path, "enable_wal", database_error())
    assert details["disk"]["errno"] == errno.EACCES
    assert details["process"]["fd_limits"]["error_type"] == "ImportError"
    assert details["process"]["descriptor_counts"]["errno"] == errno.EACCES
    assert details["paths"]["database"]["size_bytes"] == 0
    assert details["os_errno"] is None


@pytest.mark.parametrize("failure_source", ["collector", "logger"])
def test_diagnostic_failures_do_not_replace_original_error(tmp_path, monkeypatch, caplog, failure_source) -> None:
    """采集器或日志输出本身失败时仍传播原始数据库错误。"""

    error = database_error()
    monkeypatch.setattr("teamwork_review_agents.state.sqlite3.connect", Mock(side_effect=error))
    if failure_source == "collector":
        monkeypatch.setattr(sqlite_diagnostics, "collect_sqlite_diagnostics", Mock(side_effect=RuntimeError("probe failed")))
    else:
        monkeypatch.setattr(sqlite_diagnostics.LOGGER, "error", Mock(side_effect=OSError(errno.ENOSPC, "log full")))
    with pytest.raises(sqlite3.OperationalError) as caught:
        with StateStore(tmp_path / "state.db").connect():
            pass
    assert caught.value is error
    if failure_source == "collector":
        details, = diagnostics_records(caplog)
        assert details["phase"] == "open"
        assert details["sqlite_errorcode"] == error.sqlite_errorcode
        assert "probe failed" in details["diagnostics_error"]


def test_successful_transactions_do_not_collect_diagnostics(tmp_path, monkeypatch, caplog) -> None:
    """正常读写保持提交行为，并且不执行任何故障资源探针。"""

    collector = Mock()
    monkeypatch.setattr(sqlite_diagnostics, "collect_sqlite_diagnostics", collector)
    caplog.set_level(logging.ERROR, logger=sqlite_diagnostics.__name__)
    store = StateStore(tmp_path / "state.db")
    with store.connect() as connection:
        connection.execute("CREATE TABLE example (value INTEGER)")
        connection.execute("INSERT INTO example VALUES (7)")
    with store.connect() as connection:
        assert connection.execute("SELECT value FROM example").fetchone()[0] == 7
    collector.assert_not_called()
    assert diagnostics_records(caplog) == []
