"""非交互子进程隐藏窗口策略与原生 Windows 管道回归测试。"""

from __future__ import annotations

import ast
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from teamwork_review_agents import (
    codex_model_client,
    codex_settings,
    managed_sandbox,
    process_control,
    workspace_snapshot,
)
from teamwork_review_agents.process_control import process_group_options, terminate_process


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_synchronous_background_commands_apply_window_policy(
    tmp_path, monkeypatch, platform
):
    """六处同步 Git 与诊断命令均使用窗口策略，POSIX 不增加会话参数。"""

    captured = []

    def run(command, **kwargs):
        """仅记录创建参数，不在 POSIX 宿主机执行 Windows 标志。"""

        captured.append((command, kwargs))
        if "ls-files" in command:
            output = b"artifact.txt\0"
        elif "rev-parse" in command:
            output = "a" * 40 + "\n"
        elif "--bundled" in command:
            output = '{"models": []}'
        elif "sandbox" in command:
            output = "--permission-profile PROFILE"
        else:
            output = "codex-cli 1.2.3"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(process_control, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(subprocess, "run", run)
    for module in (codex_settings, codex_model_client):
        monkeypatch.setattr(
            module, "resolve_executable", lambda command, *args: command
        )
    codex_model_client._codex_client_version.cache_clear()
    managed_sandbox._inspect_cached.cache_clear()
    try:
        assert workspace_snapshot._run_git_paths(tmp_path, ignored=False) == {
            "artifact.txt"
        }
        assert workspace_snapshot._workspace_head(tmp_path) == "a" * 40
        assert codex_settings.inspect_codex_binary("fake-codex")["version"] == "1.2.3"
        assert codex_settings.read_bundled_models("fake-codex") == ([], None)
        assert codex_model_client._codex_client_version("fake-codex") == "1.2.3"
        assert managed_sandbox._inspect_cached(
            "fake-codex", None, "Windows", "windows"
        ).available
    finally:
        # 清除模拟诊断结果，避免后续测试复用虚假的全局缓存。
        codex_model_client._codex_client_version.cache_clear()
        managed_sandbox._inspect_cached.cache_clear()
    assert len(captured) == 6
    for _, options in captured:
        assert "start_new_session" not in options
        if platform == "nt":
            assert options["creationflags"] == 0x08000000
        else:
            assert "creationflags" not in options
        assert options.get("capture_output") or options.get("stdout") == subprocess.PIPE


def test_all_direct_process_launches_declare_window_policy():
    """新增启动点必须显式使用公共策略，防止漏掉低频诊断命令。"""

    source_root = Path(process_control.__file__).parent
    uncovered = []
    launches = 0
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            module = node.func.value
            if not isinstance(module, ast.Name):
                continue
            is_launch = (
                module.id == "subprocess"
                and node.func.attr
                in {"Popen", "run", "call", "check_call", "check_output"}
            ) or (
                module.id == "asyncio"
                and node.func.attr
                in {"create_subprocess_exec", "create_subprocess_shell"}
            )
            if not is_launch:
                continue
            launches += 1
            if not any(
                keyword.arg is None
                and isinstance(keyword.value, ast.Call)
                and isinstance(keyword.value.func, ast.Name)
                and keyword.value.func.id
                in {"process_group_options", "hidden_process_options"}
                for keyword in node.keywords
            ):
                uncovered.append(f"{path.name}:{node.lineno}")
    assert launches >= 6
    assert not uncovered, f"以下子进程缺少窗口创建策略：{uncovered}"


@pytest.mark.skipif(os.name != "nt", reason="仅原生 Windows 提供控制台窗口句柄")
@pytest.mark.parametrize("mode", ["sync", "group", "async", "powershell"])
def test_windows_detached_parent_starts_windowless_child_with_pipes(mode):
    """模拟后台服务，真实检查子进程无控制台且标准输入输出、退出码正常。"""

    # PowerShell 启动与 Add-Type 可能较慢，外层另留诊断和清理时间。
    child_timeout = 60 if mode == "powershell" else 10 if mode == "async" else 15
    parent_timeout = 90 if mode == "powershell" else 25
    python_probe = textwrap.dedent("""
        import ctypes
        import json
        import sys
        # 使用指针宽度返回类型，避免 64 位窗口句柄被截断。
        get_window = ctypes.windll.kernel32.GetConsoleWindow
        get_window.restype = ctypes.c_void_p
        print(json.dumps({"console_window": get_window() or 0, "input": sys.stdin.read()}))
        print("probe stderr", file=sys.stderr)
        raise SystemExit(7)
    """)
    command = [sys.executable, "-c", python_probe]
    if mode == "powershell":
        shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if shell is None:
            pytest.skip("未安装 PowerShell")
        powershell_probe = textwrap.dedent("""
            # 直接从目标 PowerShell 进程读取控制台句柄，避免只验证 Python 包装层。
            $ProgressPreference = 'SilentlyContinue'
            $ErrorActionPreference = 'Stop'
            Add-Type -Namespace Teamwork -Name Native -MemberDefinition '[System.Runtime.InteropServices.DllImport("kernel32.dll")] public static extern System.IntPtr GetConsoleWindow();'
            @{console_window=[Teamwork.Native]::GetConsoleWindow().ToInt64(); input=[Console]::In.ReadToEnd()} | ConvertTo-Json -Compress
            [Console]::Error.WriteLine('probe stderr')
            exit 7
        """)
        # 使用 PowerShell 约定的 UTF-16LE 编码，避免 Windows argv 与脚本双重转义。
        encoded_probe = base64.b64encode(powershell_probe.encode("utf-16le")).decode(
            "ascii"
        )
        command = [
            shell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded_probe,
        ]
    parent_code = textwrap.dedent("""
        import asyncio
        import ctypes
        import json
        import subprocess
        import sys
        import time
        from teamwork_review_agents.process_control import hidden_process_options, process_group_options, terminate_process

        started_at = time.monotonic()
        mode, command = sys.argv[1], json.loads(sys.argv[2])
        child_timeout = float(sys.argv[3])

        def report(stage, **details):
            # 阶段日志走父进程 stderr，不改变子进程的管道验证内容。
            print(json.dumps({
                "stage": stage, "elapsed_seconds": round(time.monotonic() - started_at, 3),
                **details,
            }), file=sys.stderr, flush=True)

        # 父进程以 DETACHED_PROCESS 启动，复现实际后台服务没有控制台的条件。
        report("check_parent_console")
        get_window = ctypes.windll.kernel32.GetConsoleWindow
        get_window.restype = ctypes.c_void_p
        parent_window = get_window() or 0
        assert not parent_window, f"后台父进程仍有控制台窗口：{parent_window}"
        report("run_child", executable=command[0], timeout_seconds=child_timeout)
        if mode == "async":
            async def execute():
                # 使用与工具命令、CI 和 MCP 相同的异步进程启动方式。
                process = await asyncio.create_subprocess_exec(
                    *command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, **process_group_options(),
                )
                # 等待超时不取消管道读取，终止后仍可收集已产生的输出。
                communication = asyncio.create_task(process.communicate(b"pipe input"))
                try:
                    stdout, stderr = await asyncio.wait_for(
                        asyncio.shield(communication), child_timeout,
                    )
                    return process.returncode, stdout.decode(), stderr.decode()
                except asyncio.TimeoutError:
                    if process.returncode is None:
                        process.kill()
                    stdout, stderr = await communication
                    report("child_timeout", timeout_seconds=child_timeout,
                           stdout=stdout.decode(errors="replace"),
                           stderr=stderr.decode(errors="replace"))
                    raise
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
            code, stdout, stderr = asyncio.run(execute())
        else:
            options = hidden_process_options() if mode == "sync" else process_group_options()
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, **options)
            try:
                stdout, stderr = process.communicate("pipe input", timeout=child_timeout)
            except subprocess.TimeoutExpired as exc:
                report("terminate_child_tree", pid=process.pid)
                # 先清理后代，避免编译器等进程继续持有输出管道，导致读取无法结束。
                terminate_process(process.pid, force=True, tree=True)
                try:
                    exc.stdout, exc.stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    report("child_pipe_cleanup_timeout")
                # TimeoutExpired 即使在文本模式下也可能携带字节串。
                stdout = exc.stdout or ""
                stderr = exc.stderr or ""
                report("child_timeout", timeout_seconds=child_timeout,
                       stdout=stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout,
                       stderr=stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr)
                raise
            finally:
                if process.poll() is None:
                    terminate_process(process.pid, force=True, tree=True)
                    process.wait(timeout=5)
            code = process.returncode
        report("child_completed", returncode=code)
        # 原始输出交给 pytest 解析，解析失败时仍能看到 PowerShell 的真正错误。
        print(json.dumps({"code": code, "stdout": stdout, "stderr": stderr.strip()}))
    """)
    started_at = time.monotonic()
    context = (
        f"模式：{mode}；程序：{command[0]}；"
        f"子进程超时：{child_timeout} 秒；父进程超时：{parent_timeout} 秒"
    )
    timed_out = False
    cleanup_error = None
    # 外层仅收集诊断，使用临时文件避免后代持有管道时 communicate 在超时后仍阻塞。
    # 真正需要验证的子进程标准输入输出仍使用上方的 PIPE。
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", parent_code, mode, json.dumps(command), str(child_timeout)],
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            **process_group_options(detached=True),
        )
        try:
            process.wait(timeout=parent_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if process.poll() is None:
                try:
                    terminate_process(process.pid, force=True, tree=True)
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    cleanup_error = str(exc)
        stdout_file.seek(0)
        stderr_file.seek(0)
        result = subprocess.CompletedProcess(
            process.args, process.returncode,
            stdout_file.read().decode("utf-8", errors="replace"),
            stderr_file.read().decode("utf-8", errors="replace"),
        )
    diagnostic = (
        f"{context}；耗时：{time.monotonic() - started_at:.3f} 秒；"
        f"父进程退出码：{result.returncode}\n"
        f"原始 stdout：\n{result.stdout}\n原始 stderr / 阶段日志：\n{result.stderr}"
    )
    if cleanup_error:
        diagnostic += f"\n进程树清理失败：{cleanup_error}"
    if timed_out:
        pytest.fail(f"测试父进程等待超时\n{diagnostic}", pytrace=False)
    assert result.returncode == 0, f"测试父进程执行失败\n{diagnostic}"
    try:
        report = json.loads(result.stdout)
        if not isinstance(report, dict):
            raise ValueError("父进程输出必须为 JSON 对象")
    except ValueError as exc:
        pytest.fail(f"父进程输出解析失败：{exc}\n{diagnostic}", pytrace=False)
    # 解开父进程的 JSON 包装，让子进程错误中的中文和换行可直接阅读。
    diagnostic += (
        f"\n子进程退出码：{report.get('code')}\n"
        f"子进程原始 stdout：\n{report.get('stdout')}\n"
        f"子进程原始 stderr：\n{report.get('stderr')}"
    )
    assert report.get("code") == 7, f"子进程未按探测脚本约定退出（预期 7）\n{diagnostic}"
    try:
        report["stdout"] = json.loads(report["stdout"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        pytest.fail(f"子进程输出解析失败：{exc}\n{diagnostic}", pytrace=False)
    assert report == {
        "code": 7,
        "stdout": {"console_window": 0, "input": "pipe input"},
        "stderr": "probe stderr",
    }, f"窗口或管道探测结果不符合预期\n{diagnostic}"
