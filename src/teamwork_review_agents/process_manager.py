"""前后台服务进程的 PID、锁和日志管理。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO


@dataclass(frozen=True)
class RuntimePaths:
    """单个配置文件对应的运行时文件路径。"""

    directory: Path
    pid_file: Path
    lock_file: Path
    log_file: Path


@dataclass(frozen=True)
class ProcessRecord:
    """写入 PID 文件的受管服务身份信息。"""

    pid: int
    config_path: str
    process_started_at: str
    host: str
    port: int
    detached: bool


@dataclass(frozen=True)
class ProcessActionResult:
    """进程管理命令的退出码与用户提示。"""

    exit_code: int
    message: str
    record: ProcessRecord | None = None


def resolve_config_path(config_path: str | Path) -> Path:
    """返回稳定的配置文件绝对路径。"""

    return Path(config_path).expanduser().resolve()


def management_url(host: str, port: int) -> str:
    """将监听地址转换为可在本机打开的管理界面地址。"""

    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}"


def runtime_paths(config_path: str | Path) -> RuntimePaths:
    """按配置文件生成 PID、进程锁与后台日志路径。"""

    resolved = resolve_config_path(config_path)
    directory = resolved.parent / "data"
    if resolved.name == "config.yaml":
        prefix = "teamwork-review-agents"
    else:
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", resolved.stem).strip("-") or "config"
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
        prefix = f"teamwork-review-agents-{stem}-{digest}"
    return RuntimePaths(
        directory=directory,
        pid_file=directory / f"{prefix}.pid",
        lock_file=directory / f"{prefix}.lock",
        log_file=directory / f"{prefix}.log",
    )


def _process_started_at(pid: int) -> str | None:
    """读取进程启动时间，用于防止 PID 重用时误杀其他进程。"""

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = " ".join(result.stdout.split())
    return value or None


def _pid_exists(pid: int) -> bool:
    """检查 PID 是否仍然存在。"""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_state(pid: int) -> str | None:
    """读取进程状态；僵尸进程视为已经结束。"""

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip().split()
    return value[0] if value else None


def _argument_value(arguments: list[str], *names: str) -> str | None:
    """读取命令行中独立参数或等号形式参数的值。"""

    for index, argument in enumerate(arguments):
        if argument in names and index + 1 < len(arguments):
            return arguments[index + 1]
        for name in names:
            prefix = f"{name}="
            if argument.startswith(prefix):
                return argument[len(prefix) :]
    return None


def _discover_managed_processes(config_path: str | Path) -> list[ProcessRecord]:
    """在运行文件失效时按完整命令身份发现同一配置的托管进程。"""

    resolved = resolve_config_path(config_path)
    try:
        result = subprocess.run(
            ["ps", "-ww", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    records: list[ProcessRecord] = []
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(.+)", line)
        if match is None:
            continue
        pid = int(match.group(1))
        if pid == os.getpid():
            continue
        try:
            arguments = shlex.split(match.group(2))
        except ValueError:
            continue
        module_positions = [
            index
            for index, argument in enumerate(arguments[:-2])
            if argument == "-m"
            and arguments[index + 1] == "teamwork_review_agents"
            and arguments[index + 2] == "run"
        ]
        if not module_positions or "--managed-child" not in arguments:
            continue
        configured = _argument_value(arguments, "-c", "--config")
        if configured is None:
            continue
        try:
            candidate_path = resolve_config_path(configured)
        except OSError:
            continue
        if candidate_path != resolved:
            continue
        started_at = _process_started_at(pid)
        state = _process_state(pid)
        if not started_at or not state or state.startswith("Z"):
            continue
        port_value = _argument_value(arguments, "--port")
        try:
            port = int(port_value) if port_value is not None else 8080
        except ValueError:
            continue
        records.append(
            ProcessRecord(
                pid=pid,
                config_path=str(resolved),
                process_started_at=started_at,
                host=_argument_value(arguments, "--host") or "127.0.0.1",
                port=port,
                detached=True,
            )
        )
    return sorted(records, key=lambda record: record.pid)


def _reap_child(pid: int) -> None:
    """当前进程恰好是父进程时回收已经退出的子进程。"""

    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return


def _read_record(paths: RuntimePaths) -> ProcessRecord | None:
    """读取并校验 PID 文件。"""

    try:
        payload = json.loads(paths.pid_file.read_text(encoding="utf-8"))
        return ProcessRecord(
            pid=int(payload["pid"]),
            config_path=str(payload["config_path"]),
            process_started_at=str(payload["process_started_at"]),
            host=str(payload["host"]),
            port=int(payload["port"]),
            detached=bool(payload.get("detached", False)),
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _record_is_running(record: ProcessRecord) -> bool:
    """确认 PID 和启动时间都与记录一致。"""

    if not _pid_exists(record.pid):
        return False
    started_at = _process_started_at(record.pid)
    state = _process_state(record.pid)
    return bool(
        started_at
        and started_at == record.process_started_at
        and state
        and not state.startswith("Z")
    )


def _lock_is_held(paths: RuntimePaths) -> bool:
    """检查服务进程是否持有配置专属锁。"""

    paths.directory.mkdir(parents=True, exist_ok=True)
    with paths.lock_file.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return False


def _running_processes(config_path: str | Path) -> list[ProcessRecord]:
    """组合 PID 记录与系统进程表，返回当前配置的全部存活实例。"""

    resolved = resolve_config_path(config_path)
    paths = runtime_paths(resolved)
    record = _read_record(paths)
    lock_held = _lock_is_held(paths)
    discovered_records = _discover_managed_processes(resolved)
    discovered_by_pid = {item.pid: item for item in discovered_records}
    records: dict[int, ProcessRecord] = {}
    if (
        record
        and record.config_path == str(resolved)
        and _record_is_running(record)
        and (lock_held or record.pid in discovered_by_pid)
    ):
        records[record.pid] = record
    for discovered in discovered_records:
        records.setdefault(discovered.pid, discovered)
    if not records and not lock_held:
        paths.pid_file.unlink(missing_ok=True)
    return sorted(records.values(), key=lambda item: item.pid)


def running_process(config_path: str | Path) -> ProcessRecord | None:
    """返回当前配置正在运行的一个服务记录，并兼容管理文件丢失。"""

    records = _running_processes(config_path)
    return records[0] if records else None


class ServiceLease:
    """服务运行期间持续持有的单实例文件锁。"""

    def __init__(
        self,
        paths: RuntimePaths,
        lock_file: IO[str],
        record: ProcessRecord,
    ) -> None:
        self.paths = paths
        self.lock_file = lock_file
        self.record = record
        self._released = False

    @classmethod
    def acquire(
        cls,
        config_path: str | Path,
        *,
        host: str,
        port: int,
        detached: bool,
    ) -> ServiceLease | None:
        """申请配置专属锁，并登记当前服务进程。"""

        resolved = resolve_config_path(config_path)
        paths = runtime_paths(resolved)
        paths.directory.mkdir(parents=True, exist_ok=True)
        lock_file = paths.lock_file.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            return None

        started_at = _process_started_at(os.getpid())
        if not started_at:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            raise RuntimeError("无法读取当前服务进程的启动时间")
        record = ProcessRecord(
            pid=os.getpid(),
            config_path=str(resolved),
            process_started_at=started_at,
            host=host,
            port=port,
            detached=detached,
        )
        temporary = paths.pid_file.with_name(f".{paths.pid_file.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, paths.pid_file)
        return cls(paths, lock_file, record)

    def release(self) -> None:
        """删除属于当前进程的 PID 文件并释放锁。"""

        if self._released:
            return
        current = _read_record(self.paths)
        if current and current.pid == self.record.pid:
            self.paths.pid_file.unlink(missing_ok=True)
        fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
        self.lock_file.close()
        self._released = True

    def __enter__(self) -> ServiceLease:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def _tail_log(path: Path, limit: int = 20) -> str:
    """读取后台日志末尾，便于展示启动失败原因。"""

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    return "\n".join(lines[-limit:])


def _health_process_id(host: str, port: int) -> int | None:
    """读取健康接口实际响应进程的 PID，连接失败时返回空。"""

    url = f"{management_url(host, port)}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload["pid"])
    except (KeyError, TypeError, ValueError):
        return None


def start_background(
    config_path: str | Path,
    *,
    host: str,
    port: int,
    startup_timeout_seconds: float = 5,
) -> ProcessActionResult:
    """启动脱离当前终端的后台服务进程。"""

    resolved = resolve_config_path(config_path)
    paths = runtime_paths(resolved)
    existing_records = _running_processes(resolved)
    if len(existing_records) > 1:
        pids = ", ".join(str(record.pid) for record in existing_records)
        return ProcessActionResult(
            1,
            f"检测到同一配置的多个后台服务进程：PID {pids}，请先执行 stop",
        )
    if existing_records:
        existing = existing_records[0]
        if _health_process_id(existing.host, existing.port) != existing.pid:
            return ProcessActionResult(
                1,
                (
                    f"检测到后台服务进程 PID {existing.pid}，但健康接口未由该进程响应；"
                    "请执行 stop 或 restart 恢复"
                ),
                existing,
            )
        return ProcessActionResult(
            0,
            (
                f"后台服务已在运行：PID {existing.pid}，"
                f"{management_url(existing.host, existing.port)}"
            ),
            existing,
        )

    paths.directory.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "teamwork_review_agents",
        "run",
        "-c",
        str(resolved),
        "--host",
        host,
        "--port",
        str(port),
        "--managed-child",
    ]
    with paths.log_file.open("a", encoding="utf-8") as log_file:
        started_text = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
        log_file.write(f"\n===== {started_text} 后台启动 =====\n")
        log_file.flush()
        process = subprocess.Popen(
            command,
            cwd=resolved.parent,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=os.environ.copy(),
        )

    deadline = time.monotonic() + startup_timeout_seconds
    while time.monotonic() < deadline:
        return_code = process.poll()
        record = _read_record(paths)
        if return_code is not None:
            log_tail = _tail_log(paths.log_file)
            detail = f"\n{log_tail}" if log_tail else ""
            return ProcessActionResult(
                1,
                (
                    f"后台服务启动失败，退出码 {return_code}。"
                    f"日志：{paths.log_file}{detail}"
                ),
            )
        if (
            record
            and record.pid == process.pid
            and _record_is_running(record)
            and _health_process_id(host, port) == process.pid
        ):
            return ProcessActionResult(
                0,
                (
                    f"后台服务已启动：PID {record.pid}\n"
                    f"管理界面：{management_url(record.host, record.port)}\n"
                    f"后台日志：{paths.log_file}"
                ),
                record,
            )
        time.sleep(0.1)

    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
    log_tail = _tail_log(paths.log_file)
    detail = f"\n{log_tail}" if log_tail else ""
    return ProcessActionResult(
        1,
        (
            f"后台服务未在 {startup_timeout_seconds:g} 秒内完成启动，"
            f"请查看日志：{paths.log_file}{detail}"
        ),
    )


def stop_managed_process(
    config_path: str | Path,
    *,
    timeout_seconds: float = 30,
) -> ProcessActionResult:
    """优雅停止当前配置全部实例，超时后再强制结束。"""

    resolved = resolve_config_path(config_path)
    paths = runtime_paths(resolved)
    records = _running_processes(resolved)
    if not records:
        return ProcessActionResult(0, "服务当前未运行")

    target_pids = {record.pid for record in records}
    for record in records:
        if not _record_is_running(record):
            continue
        try:
            os.kill(record.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            return ProcessActionResult(
                1,
                f"无权结束服务进程 PID {record.pid}：{exc}",
                record,
            )

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if all(not _record_is_running(record) for record in records):
            for record in records:
                _reap_child(record.pid)
            current = _read_record(paths)
            if current and current.pid in target_pids:
                paths.pid_file.unlink(missing_ok=True)
            pids = ", ".join(str(record.pid) for record in records)
            return ProcessActionResult(0, f"服务已停止：PID {pids}", records[0])
        time.sleep(0.1)

    forced = [record for record in records if _record_is_running(record)]
    for record in forced:
        try:
            if record.detached:
                os.killpg(record.pid, signal.SIGKILL)
            else:
                os.kill(record.pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            return ProcessActionResult(
                1,
                f"无权强制结束服务进程 PID {record.pid}：{exc}",
                record,
            )
    force_deadline = time.monotonic() + 3
    while time.monotonic() < force_deadline and any(
        _record_is_running(record) for record in forced
    ):
        time.sleep(0.1)
    for record in records:
        _reap_child(record.pid)
    current = _read_record(paths)
    if current and current.pid in target_pids:
        paths.pid_file.unlink(missing_ok=True)
    remaining = [record for record in records if _record_is_running(record)]
    if remaining:
        pids = ", ".join(str(record.pid) for record in remaining)
        return ProcessActionResult(
            1,
            f"无法结束服务进程：PID {pids}",
            remaining[0],
        )
    pids = ", ".join(str(record.pid) for record in records)
    return ProcessActionResult(
        0,
        f"服务未在 {timeout_seconds:g} 秒内完成收尾，已强制停止：PID {pids}",
        records[0],
    )
