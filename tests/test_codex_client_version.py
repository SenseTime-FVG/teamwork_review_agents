"""长生命周期客户端版本刷新、请求边界与探测失败回归。"""

from __future__ import annotations

import asyncio
import errno
import json
import subprocess
import threading

import httpx
import pytest

from teamwork_review_agents import codex_model_client
from teamwork_review_agents.codex_executable import (
    CodexExecutable,
    CodexRuntimeError,
    active_codex_executable,
)
from teamwork_review_agents.codex_model_client import (
    CodexOAuthCredentials,
    CodexResponsesClient,
)
from teamwork_review_agents.run_control import RunControl, active_run_control


class FakeOAuth:
    """只提供固定测试凭据，禁止访问宿主登录或刷新服务。"""

    def __init__(self):
        self.refreshes = 0
        self.value = CodexOAuthCredentials("test-access", "test-refresh", 0)

    async def credentials(self, *, force_refresh=False):
        """记录同一请求的凭据刷新，不改变版本。"""

        self.refreshes += int(force_refresh)
        return self.value

    def load(self):
        """模拟磁盘上与当前请求一致的凭据。"""

        return self.value


def _completed():
    """返回最小成功 SSE，确保测试经过真实 HTTP 头构造。"""

    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content='data: {"type":"response.completed","response":{"output_text":"完成"}}\n\n',
    )


async def test_same_client_refreshes_version_after_cli_upgrade(monkeypatch):
    """同一进程和同一 CLI 路径升级后，下一次请求必须携带新版。"""

    version = "0.153.4"
    probes, headers = [], []
    monkeypatch.setattr(codex_model_client, "resolve_executable", lambda command: command)

    def probe(command, **kwargs):
        """模拟原路径的 CLI 被升级，旧版不能留在永久缓存中。"""

        probes.append(command)
        return subprocess.CompletedProcess(command, 0, f"codex-cli {version}", "")

    async def handler(request):
        headers.append((request.headers["version"], request.headers["user-agent"]))
        return _completed()

    monkeypatch.setattr(codex_model_client.subprocess, "run", probe)
    client = CodexResponsesClient(
        oauth=FakeOAuth(), codex_binary="test-codex", transport=httpx.MockTransport(handler),
    )
    assert (await client.create_response({"model": "gpt-test"}))["output_text"] == "完成"
    version = "0.159.2"
    await client.create_response({"model": "gpt-test"})

    assert probes == [["test-codex", "--version"]] * 2
    assert headers == [
        ("0.153.4", "teamwork-review-agents/0.153.4"),
        ("0.159.2", "teamwork-review-agents/0.159.2"),
    ]


async def test_network_and_auth_retries_reuse_request_version(monkeypatch):
    """网络重试和 401 刷新共用版本快照，下一请求才吸收升级。"""

    version = "0.153.4"
    probes, headers = [], []

    def probe(command):
        """一次请求只能执行一次探测。"""

        probes.append(command)
        return version

    async def handler(request):
        nonlocal version
        headers.append((request.headers["version"], request.headers["user-agent"]))
        version = "0.159.2"
        if len(headers) == 1:
            return httpx.Response(503, json={"error": {"message": "暂时不可用"}})
        if len(headers) == 2:
            return httpx.Response(401, json={"error": {"message": "需要刷新凭据"}})
        return _completed()

    monkeypatch.setattr(codex_model_client, "_codex_client_version", probe)
    oauth = FakeOAuth()
    client = CodexResponsesClient(
        oauth=oauth, codex_binary="test-codex", transport=httpx.MockTransport(handler),
    )
    await client.create_response({"model": "gpt-test"})
    assert probes == ["test-codex"]
    assert oauth.refreshes == 1
    assert headers == [("0.153.4", "teamwork-review-agents/0.153.4")] * 3
    await client.create_response({"model": "gpt-test"})
    assert probes == ["test-codex"] * 2
    assert headers[-1] == ("0.159.2", "teamwork-review-agents/0.159.2")


async def test_version_probe_does_not_block_loop_and_preserves_bound_path(monkeypatch):
    """探测期间事件循环可继续运行，线程仍使用本轮绑定的程序。"""

    entered, release = threading.Event(), threading.Event()
    commands = []

    def probe(command, **kwargs):
        """用可释放的等待模拟慢 CLI，并限制失败时的等待上限。"""

        commands.append(command)
        entered.set()
        assert release.wait(3), "版本探测阻塞了事件循环"
        return subprocess.CompletedProcess(command, 0, "codex-cli 0.159.2", "")

    async def handler(request):
        return _completed()

    monkeypatch.setattr(codex_model_client.subprocess, "run", probe)
    token = active_codex_executable.set(CodexExecutable("test-codex", "/bound/codex", "test"))
    task = None
    try:
        client = CodexResponsesClient(
            oauth=FakeOAuth(), codex_binary="test-codex", transport=httpx.MockTransport(handler),
        )
        task = asyncio.create_task(client.create_response({"model": "gpt-test"}))
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, 2)
        assert commands == [["/bound/codex", "--version"]]
    finally:
        release.set()
        active_codex_executable.reset(token)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("failure", ["timeout", "os_error", "nonzero", "unrecognized"])
async def test_invalid_version_blocks_request_without_unknown_header(monkeypatch, failure):
    """无法可靠读取版本时不请求上游，也不静默降级为 unknown。"""

    requests, probes, delays, diagnostics = [], [], [], []
    monkeypatch.setattr(codex_model_client, "resolve_executable", lambda command: command)

    def probe(command, **kwargs):
        """覆盖系统异常、超时、非零退出及异常版本输出。"""

        probes.append(command)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 3)
        if failure == "os_error":
            raise OSError("测试启动失败")
        return subprocess.CompletedProcess(
            command, 1 if failure == "nonzero" else 0,
            "codex-cli 0.159.2" if failure == "nonzero" else "无法识别的输出", "",
        )

    async def handler(request):
        requests.append(request)
        return _completed()

    async def sleep(delay):
        """记录真实退避参数，但不让测试实际等待四分半。"""

        delays.append(delay)

    async def diagnose(event):
        diagnostics.append(event)

    monkeypatch.setattr(codex_model_client.asyncio, "sleep", sleep)
    monkeypatch.setattr(codex_model_client.subprocess, "run", probe)
    client = CodexResponsesClient(
        oauth=FakeOAuth(), codex_binary="test-codex", transport=httpx.MockTransport(handler),
        diagnostic_callback=diagnose,
    )
    with pytest.raises(CodexRuntimeError) as raised:
        await client.create_response({"model": "gpt-test"})
    assert raised.value.error_code == "codex_version_probe_failed"
    assert raised.value.retryable is False
    assert "未发送模型请求" in str(raised.value)
    assert len(probes) == 6
    assert delays == [10, 20, 40, 80, 120]
    assert raised.value.details["probe_attempts"] == 6
    assert raised.value.details["probe_retries_exhausted"] is True
    assert [event["next_attempt"] for event in diagnostics] == [2, 3, 4, 5, 6]
    assert requests == []


async def test_missing_executable_preserves_original_runtime_error(monkeypatch):
    """程序发现失败保留既有结构化错误，不伪装成上游不支持模型。"""

    error = CodexRuntimeError("未找到 Codex CLI", error_code="codex_not_found")

    def resolve(command):
        """只模拟程序缺失，禁止尝试真实 CLI。"""

        raise error

    monkeypatch.setattr(codex_model_client, "resolve_executable", resolve)
    client = CodexResponsesClient(oauth=FakeOAuth(), codex_binary="test-codex")
    with pytest.raises(CodexRuntimeError) as raised:
        await client.create_response({"model": "gpt-test"})
    assert raised.value is error


@pytest.mark.parametrize("failure", ["nonzero", "missing", "timeout", "windows_sharing"])
async def test_version_probe_recovers_without_sse_diagnostics_or_path_change(monkeypatch, failure):
    """升级暂不可用后吸收新版，同一路径恢复才发送当前请求。"""

    probes, delays, diagnostics, events, requests = [], [], [], [], []
    monkeypatch.setattr(codex_model_client, "resolve_executable", lambda command: command)

    def probe(command, **kwargs):
        probes.append(command)
        assert kwargs["timeout"] == 3
        if len(probes) < 3:
            if failure == "missing":
                raise FileNotFoundError("升级期间暂时缺失")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, 3, output="测试凭据不可记录")
            if failure == "windows_sharing":
                error = PermissionError(errno.EACCES, "升级文件正在占用")
                error.winerror = 32
                raise error
            return subprocess.CompletedProcess(command, 1, "测试凭据不可记录", "测试凭据不可记录")
        return subprocess.CompletedProcess(command, 0, "codex-cli 0.159.2", "")

    async def sleep(delay):
        delays.append(delay)
        assert requests == []

    async def diagnose(event):
        diagnostics.append(event)
        assert requests == []

    async def receive(event):
        events.append(event)

    async def handler(request):
        requests.append(request)
        assert request.headers["version"] == "0.159.2"
        return _completed()

    monkeypatch.setattr(codex_model_client.subprocess, "run", probe)
    monkeypatch.setattr(codex_model_client.asyncio, "sleep", sleep)
    client = CodexResponsesClient(
        oauth=FakeOAuth(), codex_binary="/bound/test-codex", transport=httpx.MockTransport(handler),
        diagnostic_callback=diagnose,
    )
    assert (await client.create_response({"model": "gpt-test"}, event_callback=receive))["output_text"] == "完成"
    assert probes == [["/bound/test-codex", "--version"]] * 3
    assert delays == [10, 20]
    assert [event["type"] for event in diagnostics] == ["runtime.codex_version_retry"] * 2 + ["runtime.codex_version_recovered"]
    assert diagnostics[-1]["attempt"] == 3
    assert "测试凭据不可记录" not in json.dumps(diagnostics, ensure_ascii=False)
    assert [event["type"] for event in events] == ["response.completed"]
    assert len(requests) == 1


@pytest.mark.parametrize("permission_errno", [errno.EACCES, errno.EPERM])
async def test_permission_denied_does_not_retry_or_leak_output(monkeypatch, permission_errno):
    """明确操作系统权限拒绝不等待，原始异常中的敏感样例不进错误。"""

    probes = []
    monkeypatch.setattr(codex_model_client, "resolve_executable", lambda command: command)

    def probe(command, **kwargs):
        probes.append(command)
        raise OSError(permission_errno, "测试凭据不可记录")

    monkeypatch.setattr(codex_model_client.subprocess, "run", probe)
    client = CodexResponsesClient(oauth=FakeOAuth(), codex_binary="test-codex")
    with pytest.raises(CodexRuntimeError) as raised:
        await client.create_response({})
    assert len(probes) == 1
    assert raised.value.retryable is False
    assert "测试凭据不可记录" not in str(raised.value)


async def test_version_mismatch_preserves_fail_fast_error(monkeypatch):
    """确定的版本不匹配不能因新增探测重试变成暂时故障。"""

    error = CodexRuntimeError("版本不匹配", error_code="codex_version_mismatch")

    def probe(command):
        raise error

    monkeypatch.setattr(codex_model_client, "_codex_client_version", probe)
    with pytest.raises(CodexRuntimeError) as raised:
        await CodexResponsesClient(oauth=FakeOAuth(), codex_binary="test-codex").create_response({})
    assert raised.value is error


async def test_cancel_during_backoff_stops_probe_and_restores_wait_state(monkeypatch):
    """真实异步退避可立即取消，不发模型请求，不遗留等待状态。"""

    entered = asyncio.Event()
    probes = []

    def probe(command):
        probes.append(command)
        raise CodexRuntimeError("版本探测暂时失败", error_code="codex_version_probe_failed")

    async def diagnose(event):
        entered.set()

    monkeypatch.setattr(codex_model_client, "_codex_client_version", probe)
    control = RunControl("test-cancel")
    token = active_run_control.set(control)
    task = None
    try:
        client = CodexResponsesClient(oauth=FakeOAuth(), codex_binary="test-codex", diagnostic_callback=diagnose)
        task = asyncio.create_task(client.create_response({}))
        await asyncio.wait_for(entered.wait(), 2)
        assert control.runtime_wait_deadline is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert probes == ["test-codex"]
        assert control.runtime_wait_deadline is None
    finally:
        active_run_control.reset(token)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_version_recovery_budget_is_bounded_even_if_diagnostic_stalls(monkeypatch):
    """诊断回调异常阻塞也不能让恢复窗口无限延长。"""

    def probe(command):
        raise CodexRuntimeError("版本探测暂时失败", error_code="codex_version_probe_failed")

    async def diagnose(event):
        await asyncio.Event().wait()

    monkeypatch.setattr(codex_model_client, "_codex_client_version", probe)
    monkeypatch.setattr(codex_model_client, "_VERSION_PROBE_WAIT_BUDGET_SECONDS", 0.05)
    client = CodexResponsesClient(oauth=FakeOAuth(), codex_binary="test-codex", diagnostic_callback=diagnose)
    with pytest.raises(CodexRuntimeError) as raised:
        await asyncio.wait_for(client.create_response({}), 1)
    assert raised.value.details["wait_budget_seconds"] == 0.05
    assert raised.value.details["probe_retries_exhausted"] is True
    assert raised.value.retryable is False
