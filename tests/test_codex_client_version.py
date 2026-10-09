"""长生命周期客户端版本刷新、请求边界与探测失败回归。"""

from __future__ import annotations

import asyncio
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

    requests, probes = [], []
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

    monkeypatch.setattr(codex_model_client.subprocess, "run", probe)
    client = CodexResponsesClient(
        oauth=FakeOAuth(), codex_binary="test-codex", transport=httpx.MockTransport(handler),
    )
    with pytest.raises(CodexRuntimeError) as raised:
        await client.create_response({"model": "gpt-test"})
    assert raised.value.error_code == "codex_version_probe_failed"
    assert raised.value.retryable is False
    assert "未发送模型请求" in str(raised.value)
    assert len(probes) == 1
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
