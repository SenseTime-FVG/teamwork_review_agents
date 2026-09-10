"""Codex 发现优先级与后台运行路径绑定回归测试。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from teamwork_review_agents import codex_executable
from teamwork_review_agents.codex_executable import (
    CodexExecutable,
    CodexRuntimeError,
    active_codex_executable,
    locate_codex_executable,
)


def test_codex_resolves_explicit_path_and_supplied_path(tmp_path):
    """明确路径优先，裸命令只搜索传入的服务 PATH。"""

    binary = Path(sys.executable).resolve()
    explicit = locate_codex_executable(str(binary), {"PATH": str(tmp_path)})
    assert explicit.resolved_path == str(binary)
    assert explicit.discovery_source == "configured_path"
    found = locate_codex_executable(binary.name, {"Path": str(binary.parent)})
    assert found.resolved_path == str(binary)
    assert found.discovery_source == "path"
    with pytest.raises(CodexRuntimeError) as raised:
        locate_codex_executable(binary.name, {"PATH": str(tmp_path)})
    assert raised.value.error_code == "codex_not_found"
    assert not raised.value.retryable


@pytest.mark.parametrize("command", ["missing/codex.exe", "custom-codex"])
def test_codex_does_not_discover_for_explicit_or_custom_command(tmp_path, monkeypatch, command):
    """错误的显式路径和自定义命令不能被桌面安装静默替换。"""

    monkeypatch.setattr(codex_executable, "_windows_host", lambda: True)
    directory = tmp_path / "OpenAI" / "Codex" / "bin" / "version"
    directory.mkdir(parents=True)
    (directory / "codex.exe").write_bytes(b"test")
    with pytest.raises(CodexRuntimeError) as raised:
        locate_codex_executable(command, {"PATH": str(tmp_path), "LOCALAPPDATA": str(tmp_path)})
    assert raised.value.error_code == "codex_not_found"


@pytest.mark.parametrize("profile_fallback", [False, True])
def test_windows_discovery_probes_newest_valid_install(tmp_path, monkeypatch, profile_fallback):
    """后台 PATH 为空时按更新时间探测，坏的新安装不阻止旧安装被使用。"""

    monkeypatch.setattr(codex_executable, "_windows_host", lambda: True)
    local = tmp_path / "AppData" / "Local"
    directory = local / "OpenAI" / "Codex" / "bin"
    candidates = []
    for index, version in enumerate(("zz-old", "aa-new"), start=1):
        binary = directory / version / "codex.exe"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"test")
        os.utime(binary, (index, index))
        candidates.append(binary)
    calls = []

    def probe(command, **kwargs):
        """只模拟平台可执行文件输出，保留真实目录发现和排序。"""

        calls.append(command[0])
        assert command[1:] == ["--version"]
        assert kwargs["env"]["PATH"] == str(tmp_path)
        if command[0] == str(candidates[1]):
            raise PermissionError("安装更新中")
        return subprocess.CompletedProcess(command, 0, "codex-cli 1.2.3", "")

    monkeypatch.setattr(codex_executable.subprocess, "run", probe)
    environment = {"PATH": str(tmp_path)}
    environment.update({"USERPROFILE": str(tmp_path)} if profile_fallback else {"LocalAppData": str(local)})
    found = locate_codex_executable("codex", environment)
    assert calls == [str(candidates[1]), str(candidates[0])]
    assert found.resolved_path == str(candidates[0].resolve())
    assert found.discovery_source == "windows_desktop"


def test_run_binding_survives_child_environment_changes(tmp_path):
    """本轮解析结果不受工具临时 HOME/PATH 变化影响，结束后恢复独立解析。"""

    expected = CodexExecutable("codex", str(tmp_path / "bound.exe"), "windows_desktop")
    token = active_codex_executable.set(expected)
    try:
        assert locate_codex_executable("codex", {"PATH": str(tmp_path)}) == expected
    finally:
        active_codex_executable.reset(token)
    with pytest.raises(CodexRuntimeError):
        locate_codex_executable("codex", {"PATH": str(tmp_path)})
