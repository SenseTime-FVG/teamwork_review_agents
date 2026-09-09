"""非交互子进程隐藏窗口策略与原生 Windows 管道回归测试。"""

from __future__ import annotations

import ast
import base64
import json
import os
import shutil
import subprocess
import sys
import textwrap
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
from teamwork_review_agents.process_control import process_group_options


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
    for module in (codex_settings, codex_model_client, managed_sandbox):
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
        from teamwork_review_agents.process_control import hidden_process_options, process_group_options

        # 父进程以 DETACHED_PROCESS 启动，复现实际后台服务没有控制台的条件。
        get_window = ctypes.windll.kernel32.GetConsoleWindow
        get_window.restype = ctypes.c_void_p
        assert not get_window()
        mode, command = sys.argv[1], json.loads(sys.argv[2])
        if mode == "async":
            async def execute():
                # 使用与工具命令、CI 和 MCP 相同的异步进程启动方式。
                process = await asyncio.create_subprocess_exec(
                    *command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, **process_group_options(),
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(b"pipe input"), 10)
                    return process.returncode, stdout.decode(), stderr.decode()
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
            code, stdout, stderr = asyncio.run(execute())
        else:
            options = hidden_process_options() if mode == "sync" else process_group_options()
            completed = subprocess.run(command, input="pipe input", capture_output=True,
                                       text=True, timeout=15, **options)
            code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        print(json.dumps({"code": code, "stdout": json.loads(stdout), "stderr": stderr.strip()}))
    """)
    result = subprocess.run(
        [sys.executable, "-c", parent_code, mode, json.dumps(command)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=25,
        check=True,
        **process_group_options(detached=True),
    )
    assert json.loads(result.stdout) == {
        "code": 7,
        "stdout": {"console_window": 0, "input": "pipe input"},
        "stderr": "probe stderr",
    }
