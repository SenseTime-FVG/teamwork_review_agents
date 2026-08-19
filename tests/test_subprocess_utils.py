"""受控子进程环境与命令解析测试。"""

from __future__ import annotations

import os

import pytest

from teamwork_review_agents.subprocess_utils import (
    remove_environment_names,
    resolve_executable,
    selected_environment,
)


def test_selected_environment_normalizes_windows_names() -> None:
    """Windows 混合大小写的系统变量应以稳定键名传给子进程。"""

    selected = selected_environment(
        {"PATH", "SYSTEMROOT", "TEMP"},
        {
            "Path": "/tools",
            "SystemRoot": "C:/Windows",
            "TEMP": "C:/Temp",
            "SECRET": "不得继承",
        },
    )

    assert selected == {
        "PATH": "/tools",
        "SYSTEMROOT": "C:/Windows",
        "TEMP": "C:/Temp",
    }


def test_selected_environment_respects_explicit_empty_source(monkeypatch) -> None:
    """显式空环境不能意外回退并继承宿主机变量。"""

    monkeypatch.setenv("SYSTEMROOT", "C:/Windows")

    assert selected_environment({"SYSTEMROOT"}, {}) == {}


def test_remove_environment_names_is_case_insensitive() -> None:
    """Provider 和模型凭据在 Windows 大小写语义下也必须移除。"""

    environment = {
        "Github_Token": "secret",
        "openai_api_key": "secret",
        "VISIBLE": "ok",
    }

    remove_environment_names(environment, ("GITHUB_TOKEN", "OPENAI_API_KEY"))

    assert environment == {"VISIBLE": "ok"}


def test_resolve_executable_uses_child_path(tmp_path) -> None:
    """普通可执行文件应按最终子进程 PATH 解析为绝对路径。"""

    if os.name == "nt":
        executable = tmp_path / "test-command.cmd"
        executable.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    else:
        executable = tmp_path / "test-command"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)

    resolved = resolve_executable(
        "test-command",
        {"PATH": str(tmp_path)},
    )

    assert resolved.lower() == str(executable.resolve()).lower()


@pytest.mark.skipif(os.name != "nt", reason="只验证 Windows PATHEXT shim")
def test_resolve_executable_finds_windows_cmd_shim(tmp_path) -> None:
    """Windows 上 npm 安装的 `.cmd` 命令应能从无扩展名称解析。"""

    shim = tmp_path / "test-command.cmd"
    shim.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")

    resolved = resolve_executable("test-command", {"PATH": str(tmp_path)})

    assert resolved.lower() == str(shim.resolve()).lower()
