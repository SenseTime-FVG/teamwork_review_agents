"""验证普通结果完整返回、大结果补读、安全保存及进程捕获保护。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from teamwork_review_agents.config import ContextCompactionConfig
from teamwork_review_agents.context_compaction import SUMMARY_REQUEST, estimate_tokens
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.managed_sandbox import ProcessLaunch, permission_profile_override
from teamwork_review_agents.model_tools import ModelToolExecutor
from teamwork_review_agents.tool_results import ToolResultError, ToolResultStore


def _store(tmp_path, secrets=()):
    """模拟服务创建的独立运行根，避免写入宿主配置或工作区。"""

    runtime = tmp_path / "运行 目录"
    runtime.mkdir(exist_ok=True)
    return ToolResultStore(runtime, SecretRedactor(secrets))


def _large_text():
    """首尾与中间各有标记，检测缺失证据而不只核对长度。"""

    return json.dumps({"exit_code": 1, "stdout": "起点\n" + '汉字"\\\n' * 5000 + "中间证据" + "x" * 40000 + "终点", "stderr": "错误详情"}, ensure_ascii=False)


def test_old_budget_is_ignored_and_normal_output_is_complete(tmp_path):
    """旧的 4KB 配置不再导致普通输出丢失中间内容。"""

    settings = ContextCompactionConfig(tool_output_tokens=4096)
    assert settings.tool_output_inline_bytes == 65536
    assert "tool_output_tokens" not in settings.model_dump()
    raw = json.dumps({"stdout": "汉字\n" * 1000}, ensure_ascii=False)
    store = _store(tmp_path)
    output, reference = store.prepare(raw, inline_bytes=settings.tool_output_inline_bytes, available_bytes=100000)
    assert output == raw and reference is None
    assert not store.directory.exists()


@pytest.mark.parametrize("available", [100000, 2500])
def test_artifact_keeps_every_byte_and_metadata(tmp_path, available):
    """缩小预览不会改变完整文件、退出码和校验信息。"""

    raw = _large_text()
    store = _store(tmp_path)
    output, reference = store.prepare(raw, inline_bytes=65536, available_bytes=available)
    result = json.loads(output)
    assert estimate_tokens(output) <= available
    assert result["exit_code"] == 1
    assert result["output_file"] == reference
    assert "不是完整证据" in result["note"] and "重跑" in result["note"]
    path = Path(reference["path"])
    assert path.parent == store.directory and path.is_absolute()
    assert path.read_bytes() == raw.encode("utf-8")
    assert reference["bytes"] == len(raw.encode("utf-8"))
    assert reference["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "中间证据" in json.loads(path.read_text(encoding="utf-8"))["stdout"]
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert "工具结果文件路径" in SUMMARY_REQUEST


def test_small_window_uses_reference_instead_of_losing_result(tmp_path):
    """正文未超过阈值但小模型窗口容纳不了时，同样保留可补读文件。"""

    raw = json.dumps({"stdout": "x" * 30000})
    output, reference = _store(tmp_path).prepare(raw, inline_bytes=65536, available_bytes=5000)
    assert reference is not None and estimate_tokens(output) <= 5000
    assert Path(reference["path"]).read_text(encoding="utf-8") == raw


def test_redacted_file_has_no_escaped_secret(tmp_path):
    """正文、日志和补读文件必须使用序列化前已脱敏的同一份数据。"""

    secret = '测试令牌"\\\nsecret'
    store = _store(tmp_path, [secret])
    raw = store.redactor.json({"stdout": secret + "x" * 70000})
    output, reference = store.prepare(raw, inline_bytes=65536, available_bytes=10000)
    saved = Path(reference["path"]).read_text(encoding="utf-8")
    assert secret not in json.loads(saved)["stdout"]
    assert json.dumps(secret, ensure_ascii=False)[1:-1] not in saved + output
    assert "********" in saved


@pytest.mark.parametrize("failure", ["quota", "disk", "budget"])
def test_storage_failure_is_explicit_and_nonretryable(tmp_path, monkeypatch, failure):
    """保存失败不能降为不完整正文，也不能自动重跑产生副作用的命令。"""

    store = _store(tmp_path)
    if failure == "quota":
        monkeypatch.setattr("teamwork_review_agents.tool_results._RUN_FILE_LIMIT_BYTES", 100)
    if failure == "disk":
        def denied(**kwargs):
            """模拟磁盘满或拒绝创建文件。"""
            raise OSError("磁盘不可写")
        monkeypatch.setattr("teamwork_review_agents.tool_results.tempfile.mkstemp", denied)
    with pytest.raises(ToolResultError) as caught:
        store.prepare(_large_text(), inline_bytes=65536, available_bytes=1 if failure == "budget" else 10000)
    assert caught.value.retryable is False
    assert caught.value.error_code == ("tool_output_reference_too_large" if failure == "budget" else "tool_output_storage_failed")
    if failure != "budget":
        assert not list(store.root.rglob("*.json"))


def test_partial_file_is_removed_after_write_failure(tmp_path, monkeypatch):
    """已独占创建但写入失败的文件不能冒充完整结果。"""

    store = _store(tmp_path)

    def denied(descriptor, mode):
        """关闭测试文件描述符后模拟写入器初始化失败。"""
        os.close(descriptor)
        raise OSError("模拟写入失败")

    monkeypatch.setattr("teamwork_review_agents.tool_results.os.fdopen", denied)
    with pytest.raises(ToolResultError):
        store.prepare(_large_text(), inline_bytes=65536, available_bytes=10000)
    assert list(store.directory.iterdir()) == []


def test_result_directory_cannot_redirect_to_other_directory(tmp_path):
    """拒绝 Agent 在服务写入前放置的结果目录链接。"""

    store = _store(tmp_path)
    outside = tmp_path / "其他目录"
    outside.mkdir()
    try:
        store.directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 账户没有创建符号链接权限")
    with pytest.raises(ToolResultError, match="符号链接"):
        store.prepare(_large_text(), inline_bytes=65536, available_bytes=10000)
    assert list(outside.iterdir()) == []


@pytest.fixture
def executor(configured_app_factory, tmp_path):
    """使用测试工作区和本机 Python，不联网、不读取任何真实运行配置。"""

    config = configured_app_factory()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    agent = config.agents["code-reviewer"].model_copy(update={"sandbox": "danger-full-access"})
    return ModelToolExecutor(config=config, agent=agent, repository=config.repositories[0], context=None,
        environment=dict(os.environ), managed_sandbox=False, cancel_check=None,
        progress_callback=lambda: None, invoke_agent_callback=None, codex_runtime_directory=runtime)


def _python_command(script):
    """按真实宿主 Shell 引用参数，兼容包含空格和中文的测试路径。"""

    argv = [sys.executable, "-I", "-c", script]
    if os.name == "nt":
        return "& " + " ".join("'" + part.replace("'", "''") + "'" for part in argv)
    return shlex.join(argv)


async def test_existing_command_tool_reads_missing_middle(executor):
    """不新增工具，使用 execute_command 在下一轮按范围补读中间证据。"""

    store = ToolResultStore(executor.codex_runtime_directory, SecretRedactor(()))
    raw = _large_text()
    _, reference = store.prepare(raw, inline_bytes=65536, available_bytes=10000)
    script = (f"import json,pathlib; d=json.loads(pathlib.Path({reference['path']!r}).read_text(encoding='utf-8')); "
              "s=d['stdout']; n=s.index('中间证据'); print(s[n:n+20])")
    # Python 输出统一 UTF-8，避免 Windows 控制台编码掩盖补读断言。
    script = "import sys; sys.stdout.reconfigure(encoding='utf-8'); " + script
    result = await executor.execute("execute_command", {"command": _python_command(script)})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "中间证据" + "x" * 16
    assert result["truncated"] is False


@pytest.mark.parametrize("windows_separation", [False, True])
def test_read_only_agent_can_read_runtime_files(executor, monkeypatch, verified_test_sandbox_python, windows_separation):
    """模拟已通过运行前验证的沙盒，检查两种平台策略下的精确运行目录授权。"""

    monkeypatch.setattr("teamwork_review_agents.managed_sandbox.windows_environment_separation", lambda: windows_separation)
    agent = executor.config.agents["code-reviewer"]
    profile = permission_profile_override(agent, codex_runtime_directory=executor.codex_runtime_directory)
    assert f'{json.dumps(str(executor.codex_runtime_directory.resolve()))}="write"' in profile
    if windows_separation:
        for directory in verified_test_sandbox_python.readable_directories:
            assert f'{json.dumps(str(Path(directory).resolve()))}="read"' in profile


async def test_command_output_over_one_megabyte_is_complete(executor):
    """原有底层 1MB 截断也被移除，标准输出及标准错误均完整返回。"""

    script = "import sys; sys.stdout.write('x'*1100000+'MIDDLE'+'y'*1100000); sys.stderr.write('tail')"
    result = await executor._run_process(ProcessLaunch([sys.executable, "-I", "-c", script], executor.environment),
        cwd=executor.repository.workspace, timeout_seconds=15)
    assert result["stdout"] == "x" * 1100000 + "MIDDLE" + "y" * 1100000
    assert result["stderr"] == "tail" and result["exit_code"] == 0
    assert result["truncated"] is False


async def test_capture_limit_terminates_without_pipe_deadlock(executor, monkeypatch):
    """无限输出触顶后继续排空直到子进程退出，不残留进程或返回假成功。"""

    monkeypatch.setattr("teamwork_review_agents.model_tools.TOOL_CAPTURE_LIMIT_BYTES", 200000)
    script = "import sys\nwhile True: sys.stdout.write('x'*65536); sys.stdout.flush()"
    with pytest.raises(ToolResultError) as caught:
        await asyncio.wait_for(executor._run_process(ProcessLaunch([sys.executable, "-I", "-c", script], executor.environment),
            cwd=executor.repository.workspace, timeout_seconds=15), timeout=25)
    assert caught.value.error_code == "tool_output_capture_limit"
    assert caught.value.retryable is False


async def test_timeout_and_cancellation_still_drain_process(executor):
    """输出保护改造不影响命令超时与取消后的进程回收。"""

    launch = ProcessLaunch([sys.executable, "-I", "-c", "import time; print('ready',flush=True); time.sleep(60)"], executor.environment)
    timed_out = await executor._run_process(launch, cwd=executor.repository.workspace, timeout_seconds=1)
    assert timed_out["timed_out"] and timed_out["exit_code"] != 0
    started = asyncio.Event()
    executor.progress_callback = started.set
    running = asyncio.create_task(executor._run_process(launch, cwd=executor.repository.workspace, timeout_seconds=60))
    await asyncio.wait_for(started.wait(), timeout=10)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=20)
