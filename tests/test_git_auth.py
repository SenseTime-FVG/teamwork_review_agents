"""以真实 Git 和虚构凭据覆盖宿主 askpass、临时文件生命周期及错误脱敏。"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import shlex
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, quote_plus
from unittest.mock import Mock

import pytest

from teamwork_review_agents import git_auth, workspace
from teamwork_review_agents.git_auth import (
    GitCredentialContext, current_git_environment, git_credential_context,
    safe_git_error_detail, write_askpass_launcher,
)
from teamwork_review_agents.workspace import WorkspaceError, _run_git


TEST_TOKEN = "test-git-token-without-real-access"
TEST_SHA = "a" * 40


@pytest.fixture
def git_environment(tmp_path, monkeypatch):
    """隔离宿主 Git 配置和凭据，临时入口固定放在中文空格路径中。"""

    if not shutil.which("git"):
        pytest.skip("未安装 Git，无法运行原生凭据调用测试")
    for key in tuple(os.environ):
        if key.startswith(("GIT_", "TEAMWORK_GIT_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.chdir(tmp_path)
    helper_root = tmp_path / "凭据 空格 & '目录"
    helper_root.mkdir()
    original_mkdtemp = git_auth.tempfile.mkdtemp
    monkeypatch.setattr(git_auth.tempfile, "mkdtemp", lambda **kwargs: original_mkdtemp(dir=helper_root, **kwargs))
    return helper_root


@pytest.fixture
def git_http_server():
    """只在本地接受虚构 Basic 凭据，提供 Git 可读取的最小远端引用。"""

    state = SimpleNamespace(require_auth=True, accepted=[], challenged=0)
    allowed = {
        "Basic " + base64.b64encode(f"{username}:{TEST_TOKEN}".encode()).decode(): username
        for username in ("x-access-token", "oauth2")
    }

    class Handler(BaseHTTPRequestHandler):
        """用原生 HTTP 挑战触发 Git askpass，不依赖真实平台或 Token。"""

        def do_GET(self):
            """返回哑协议引用；未知地址拒绝，禁止调用任何远端服务。"""

            authorization = self.headers.get("Authorization", "")
            if state.require_auth and authorization not in allowed:
                state.challenged += 1
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="test-git"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            state.accepted.append(allowed.get(authorization))
            if self.path.split("?", 1)[0] == "/repo.git/info/refs":
                body = f"{TEST_SHA}\trefs/heads/main\n".encode()
            elif self.path == "/repo.git/HEAD":
                body = b"ref: refs/heads/main\n"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            """测试不输出请求头或任何凭据。"""

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/repo.git", state
        finally:
            server.shutdown()
            worker.join(timeout=5)


@pytest.mark.parametrize("kind,username", [("github", "x-access-token"), ("gitlab", "oauth2")])
def test_real_git_credential_fill_uses_executable_path(git_environment, kind, username):
    """真实 Git 必须启动入口并得到正确凭据，不能只断言环境变量存在。"""

    with GitCredentialContext(TEST_TOKEN, provider_kind=kind) as context:
        launcher = Path(context.environment["GIT_ASKPASS"])
        assert launcher.is_absolute() and launcher.is_file()
        assert " " in str(launcher)
        assert all(TEST_TOKEN not in path.read_text(encoding="utf-8") for path in launcher.parent.iterdir())
        assert " -I -S " in launcher.read_text(encoding="utf-8")
        environment = {**os.environ, **context.environment, "PYTHONPATH": "/untrusted-import-path"}
        result = subprocess.run(
            ["git", "-c", "credential.helper=", "credential", "fill"],
            input="protocol=https\nhost=example.test\n\n", env=environment,
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        assert values["username"] == username
        assert values["password"] == TEST_TOKEN
        assert TEST_TOKEN not in str(result.args)
    assert not launcher.parent.exists()
    assert current_git_environment() is None


@pytest.mark.parametrize("kind,username", [("github", "x-access-token"), ("gitlab", "oauth2")])
def test_real_ls_remote_completes_authentication(git_environment, git_http_server, kind, username):
    """工作区 Git 在本地认证挑战后使用仓库凭据，且日志没有 Token。"""

    remote, state = git_http_server
    progress = []
    with git_credential_context(TEST_TOKEN, provider_kind=kind) as context:
        directory = Path(context.environment["GIT_ASKPASS"]).parent
        result = _run_git(["ls-remote", "--", remote, "HEAD"], timeout_seconds=10, progress_callback=progress.append)
    assert result.stdout.strip() == f"{TEST_SHA}\tHEAD"
    assert state.challenged >= 1 and username in state.accepted
    assert TEST_TOKEN not in str(result.args) + str(progress)
    assert not directory.exists()


def test_anonymous_remote_without_token(git_environment, git_http_server):
    """无凭据公开远端继续匿名读取，不创建 helper。"""

    remote, state = git_http_server
    state.require_auth = False
    with git_credential_context("", provider_kind="github") as context:
        assert context.environment == {} and current_git_environment() is None
        assert TEST_SHA in _run_git(["ls-remote", remote, "HEAD"], timeout_seconds=10).stdout
    assert state.challenged == 0
    assert not list(git_environment.iterdir())


def test_rejected_credentials_are_cleaned(git_environment, git_http_server):
    """认证失败保留安全错误，退出上下文后清理临时入口。"""

    remote, _ = git_http_server
    with pytest.raises(WorkspaceError) as captured:
        with git_credential_context("invalid-test-token", provider_kind="github") as context:
            directory = Path(context.environment["GIT_ASKPASS"]).parent
            _run_git(["ls-remote", remote, "HEAD"], timeout_seconds=10)
    assert "invalid-test-token" not in str(captured.value)
    assert not directory.exists() and current_git_environment() is None


@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
def test_interrupted_git_cleans_helper(git_environment, outcome):
    """超时和取消不遗留临时凭据文件，不依赖慢网络或长等待。"""

    with pytest.raises(WorkspaceError):
        with git_credential_context(TEST_TOKEN, provider_kind="github") as context:
            directory = Path(context.environment["GIT_ASKPASS"]).parent
            _run_git(["--version"], timeout_seconds=0 if outcome == "timeout" else 10,
                     cancel_check=(lambda: True) if outcome == "cancel" else None)
    assert not directory.exists() and current_git_environment() is None


@pytest.mark.parametrize("stage", ["write_askpass_helper", "write_askpass_launcher"])
def test_creation_failure_cleans_directory(git_environment, monkeypatch, stage):
    """进入 with 之前创建失败时，也必须清理本次目录。"""

    def fail(*_):
        """模拟临时文件创建阶段的系统错误。"""
        raise OSError("模拟 helper 创建失败")

    monkeypatch.setattr(git_auth, stage, fail)
    with pytest.raises(OSError, match="模拟 helper 创建失败"):
        GitCredentialContext(TEST_TOKEN, provider_kind="github").start()
    assert not list(git_environment.iterdir())
    assert current_git_environment() is None


@pytest.mark.asyncio
async def test_contexts_restore_after_concurrent_and_nested_runs(git_environment):
    """并发工作线程获得各自凭据，嵌套异常后仍恢复父上下文。"""

    async def child(token):
        """让异步上下文重叠，验证线程传播及嵌套恢复。"""
        with git_credential_context(token, provider_kind="github") as context:
            await asyncio.sleep(0)
            environment = await asyncio.to_thread(current_git_environment)
            assert environment["TEAMWORK_GIT_TOKEN"] == token
            with pytest.raises(RuntimeError):
                with git_credential_context("nested-test-token", provider_kind="gitlab"):
                    raise RuntimeError("模拟嵌套运行失败")
            assert current_git_environment() is context.environment
            return Path(environment["GIT_ASKPASS"]).parent

    directories = await asyncio.gather(child("first-test-token"), child("second-test-token"))
    assert directories[0] != directories[1]
    assert all(not path.exists() for path in directories)
    assert current_git_environment() is None


def test_windows_launcher_quotes_python_and_helper(tmp_path, monkeypatch):
    """跨平台生成入口时保留 Windows 空格路径和 shell 特殊字符。"""

    monkeypatch.setattr(git_auth, "os", SimpleNamespace(name="nt"))
    command = [r"C:\Program Files\Python\python.exe", "-I", "-S", r"D:\中文 & '目录\askpass.py"]
    launcher = tmp_path / "askpass.sh"
    write_askpass_launcher(launcher, command)
    content = launcher.read_text(encoding="utf-8")
    assert launcher.read_bytes().startswith(b"#!/bin/sh\n")
    assert b"\r\n" not in launcher.read_bytes()
    arguments = shlex.split(content.splitlines()[-1])
    assert arguments == ["exec", *[part.replace("\\", "/") for part in command], "$@"]


def test_error_redaction_preserves_diagnostic_without_credentials():
    """错误包含编码密钥、代理认证和控制序列时仍不能暴露秘密。"""

    token = "dummy secret/+value"
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    raw = (
        f"\x1b[31mfatal: cannot exec askpass.sh: No such file or directory\x1b[0m\n"
        f"{token} {quote(token, safe='')} {quote_plus(token, safe='')} {encoded}\n"
        "https://proxy-user:proxy-password@proxy.test:8080/path?key=query-secret#fragment-secret\n"
        "Proxy-Authorization: Basic unknown-encoded-secret\n"
        "Authorization: Bearer unknown-bearer-secret\n"
        "password='password secret' api_key=key-secret\x00\u202e"
    )
    detail = safe_git_error_detail(raw, secrets=(token,))
    assert "fatal: cannot exec askpass.sh: No such file or directory" in detail
    assert "https://proxy.test:8080/path" in detail
    for value in (token, quote(token, safe=''), quote_plus(token, safe=''), encoded,
                  "proxy-user", "proxy-password", "query-secret", "fragment-secret", "unknown-encoded-secret",
                  "unknown-bearer-secret", "password secret", "key-secret", "\x1b", "\x00", "\u202e"):
        assert value not in detail
    assert len(detail) <= 800


def test_errors_are_redacted_before_truncation():
    """长错误不能在截断后遗留无法匹配的半截密钥。"""

    token = "fake-token-" + "a" * 900
    detail = safe_git_error_detail("x" * 1000 + token + "\nfatal: denied", secrets=(token,))
    assert len(detail) == 800
    assert "aaaaaaaa" not in detail
    assert detail.endswith("********\nfatal: denied")


def test_workspace_error_and_progress_share_redaction(git_environment, monkeypatch):
    """原生 Git 失败时，异常与可持久化进度必须同时使用脱敏摘要。"""

    encoded = base64.b64encode(f"oauth2:{TEST_TOKEN}".encode()).decode()
    process = Mock(returncode=128)
    process.poll.return_value = 128
    process.stdout = io.BytesIO()
    process.stderr = io.BytesIO((
        f"fatal: Authentication failed\n{TEST_TOKEN} {encoded}\n"
        "https://proxy-user:proxy-password@proxy.test/?key=query-secret"
    ).encode())
    monkeypatch.setattr(workspace.subprocess, "Popen", Mock(return_value=process))
    progress = []
    with pytest.raises(WorkspaceError) as captured:
        with git_credential_context(TEST_TOKEN, provider_kind="gitlab"):
            _run_git(["ls-remote", "https://example.test/repo.git"], progress_callback=progress.append)
    assert progress[-1].state == "failed"
    assert "Authentication failed" in str(captured.value) and "Authentication failed" in progress[-1].error
    exported = str(captured.value) + str([event.as_dict() for event in progress])
    assert all(secret not in exported for secret in (TEST_TOKEN, encoded, "proxy-password", "query-secret"))
