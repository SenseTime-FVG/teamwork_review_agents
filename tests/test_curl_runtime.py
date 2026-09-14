"""固定分发、离线初始化、后台生命周期与管理 API 的回归。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import stat
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from teamwork_review_agents import curl_distribution as distribution_module
from teamwork_review_agents import curl_runtime
from teamwork_review_agents.config_manager import ConfigManager
from teamwork_review_agents.curl_distribution import (
    CurlDistribution, CurlPreparationError, archive_entries, curl_runtime_root,
    distribution_for_machine, download_archive, ensure_cache_directories, installed_candidate,
    publish_archive, read_verified_archive,
)
from teamwork_review_agents.curl_runtime import CurlRuntimeManager
from teamwork_review_agents.runtime import BackgroundRuntime
from teamwork_review_agents.sandbox_curl import CurlCandidate, curl_candidates
from teamwork_review_agents.webapp import create_app

_HTTP_CLIENT = httpx.AsyncClient


def make_zip(package="curl-test", extra=None):
    """构造不含真实程序的小型归档，仅测试分发与完整性，不在宿主执行。"""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in {
            "bin/curl.exe": b"not-an-executable", "bin/curl-ca-bundle.crt": b"dummy-ca",
            "bin/libcurl.dll": b"dummy-dll", "COPYING.txt": b"dummy-license",
            **(extra or {}),
        }.items():
            archive.writestr(f"{package}/{name}", content)
    data = buffer.getvalue()
    return CurlDistribution(package, hashlib.sha256(data).hexdigest()), data


@pytest.fixture
def deployment(configured_app_factory, monkeypatch):
    """使用固定测试归档与假沙盒探针，避免自动 CI 下载或依赖真实 Codex。"""

    config = configured_app_factory()
    distribution, data = make_zip()
    monkeypatch.setattr(distribution_module, "_DISTRIBUTIONS", {"amd64": distribution, "arm64": distribution})
    monkeypatch.setattr(curl_runtime, "windows_curl_runtime_enabled", lambda: True)
    monkeypatch.setattr(curl_runtime, "curl_candidates", lambda *args: [])
    probe = AsyncMock(return_value={"code": "ready", "backend": "LibreSSL/test"})
    monkeypatch.setattr(curl_runtime, "inspect_installed_curl", probe)
    root = curl_runtime_root(config)
    return config, root, distribution, data, probe


def write_offline(root, distribution, data):
    """模拟部署人员放入同名官方 ZIP；文件内容仍必须通过固定摘要校验。"""

    ensure_cache_directories(root)
    path = root / "downloads" / distribution.filename
    path.write_bytes(data)
    return path


def mock_download(monkeypatch, data, *, status=200, headers=None):
    """拦截公共下载，不允许测试访问网络或发送业务认证。"""

    requests = []

    def serve(request):
        requests.append(request)
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(status, content=data, headers=headers)

    original = _HTTP_CLIENT
    transport = httpx.MockTransport(serve)
    monkeypatch.setattr(distribution_module.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, trust_env=False, **kwargs))
    return requests


def test_fixed_distributions_and_unknown_architecture():
    """固定架构、摘要与 URL，未知架构不猜测 x64。"""

    for machine in ("AMD64", "x86_64", "ARM64", "aarch64"):
        distribution = distribution_for_machine(machine)
        assert len(distribution.sha256) == 64
        assert "/dl-8.22.0_1/" in distribution.url
        assert "latest" not in distribution.url
    with pytest.raises(CurlPreparationError):
        distribution_for_machine("i386")


def test_offline_publish_and_corruption_detection(deployment):
    """归档、程序、CA 与 DLL 都有完整性约束，不能靠 ready 文件冒充验证。"""

    _, root, distribution, data, _ = deployment
    archive = write_offline(root, distribution, data)
    executable, ca = publish_archive(root, distribution, archive)
    assert installed_candidate(root, distribution) == (executable, ca)
    assert (executable.parent.parent / "COPYING.txt").is_file()
    assert curl_candidates(None, {}, root)[0].source == "managed_cache"
    executable.write_bytes(b"tampered")
    assert installed_candidate(root, distribution) is None
    publish_archive(root, distribution, archive)
    assert installed_candidate(root, distribution) is not None
    assert list(root.glob(".*.invalid-*"))
    (executable.parent / "unexpected.dll").write_bytes(b"injected")
    assert installed_candidate(root, distribution) is None
    assert not list(root.glob(".install-*"))


@pytest.mark.parametrize("name", ["../escape", "/absolute", "bin/../bad", "bin/a:stream", "bin/NUL", "bin/x.", "bin\\evil"])
def test_archive_rejects_unsafe_windows_paths(name):
    """即使归档摘要来自测试可信清单，也不能允许路径穿越或 Windows 设备名。"""

    distribution, data = make_zip(extra={name: b"bad"})
    with zipfile.ZipFile(io.BytesIO(data)) as archive, pytest.raises(CurlPreparationError):
        archive_entries(archive, distribution)


def test_archive_rejects_symlink_and_expansion_limit(monkeypatch):
    """链接和解压炸弹必须在写入任何安装文件前被拒绝。"""

    distribution, data = make_zip()
    buffer = io.BytesIO(data)
    with zipfile.ZipFile(buffer, "a") as archive:
        link = zipfile.ZipInfo(f"{distribution.package}/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../outside")
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive, pytest.raises(CurlPreparationError):
        archive_entries(archive, distribution)
    monkeypatch.setattr(distribution_module, "_MAX_EXPANDED", 1)
    with zipfile.ZipFile(io.BytesIO(data)) as archive, pytest.raises(CurlPreparationError):
        archive_entries(archive, distribution)


async def test_download_verifies_digest_and_never_follows_redirect(deployment, monkeypatch):
    """固定摘要通过后才能发布 ZIP，重定向不能偷偷切换分发源。"""

    _, root, distribution, data, _ = deployment
    ensure_cache_directories(root)
    requests = mock_download(monkeypatch, data)
    archive = await download_archive(root, distribution)
    assert read_verified_archive(archive, distribution) == data
    assert str(requests[0].url) == distribution.url
    assert not list((root / "downloads").glob(".download-*"))
    requests = mock_download(monkeypatch, b"", status=302, headers={"Location": "https://evil.test/archive.zip"})
    with pytest.raises(CurlPreparationError, match="下载失败"):
        await download_archive(root, distribution)
    assert len(requests) == 1


@pytest.mark.parametrize("oversized", [False, True])
async def test_bad_download_cannot_be_published(deployment, monkeypatch, oversized):
    """截断、篡改或过大下载均不能留下可复用的归档。"""

    _, root, distribution, data, _ = deployment
    ensure_cache_directories(root)
    mock_download(monkeypatch, data if oversized else b"tampered")
    if oversized:
        monkeypatch.setattr(distribution_module, "_MAX_DOWNLOAD", 2)
    with pytest.raises(CurlPreparationError):
        await download_archive(root, distribution)
    assert not (root / "downloads" / distribution.filename).exists()
    assert not list((root / "downloads").glob(".download-*"))


async def test_manager_downloads_once_and_reuses_verified_cache(deployment, monkeypatch):
    """第一次准备下载一次，重复点击与下次启动只复用并重新验证程序。"""

    config, root, distribution, data, probe = deployment
    requests = mock_download(monkeypatch, data)
    manager = CurlRuntimeManager(lambda: config)
    assert manager.start()["status"] == "preparing"
    task = manager._task
    manager.start(retry=True)
    assert manager._task is task
    await manager.wait()
    assert manager.snapshot()["status"] == "ready"
    assert len(requests) == 1
    assert probe.await_count == 1
    await manager.close()
    restarted = CurlRuntimeManager(lambda: config)
    restarted.start()
    await restarted.wait()
    assert restarted.snapshot()["status"] == "ready"
    assert len(requests) == 1
    assert probe.await_count == 2
    assert installed_candidate(root, distribution) is not None
    await restarted.close()


async def test_offline_prepare_and_bad_archive_quarantine(deployment, monkeypatch):
    """离线包走完全相同的校验；坏包保留隔离副本，不静默使用。"""

    config, root, distribution, data, _ = deployment
    archive = write_offline(root, distribution, b"bad-offline")
    download = AsyncMock(side_effect=AssertionError("已有离线包时不能自动联网"))
    monkeypatch.setattr(curl_runtime, "download_archive", download)
    manager = CurlRuntimeManager(lambda: config)
    manager.start()
    await manager.wait()
    assert manager.snapshot()["error_code"] == "archive_integrity_failed"
    assert not archive.exists()
    assert list(archive.parent.glob(".*.invalid-*"))
    archive.write_bytes(data)
    manager.start(retry=True)
    await manager.wait()
    assert manager.snapshot()["source"] == "offline_or_cached_archive"
    download.assert_not_called()
    await manager.close()


@pytest.mark.parametrize("mode", ["non_windows", "disabled", "explicit", "permission_failure", "existing"])
async def test_existing_or_explicit_programs_do_not_trigger_unwanted_download(deployment, monkeypatch, mode):
    """显式路径、平台禁用或权限故障不能被自动下载悄悄替代。"""

    config, root, _, _, probe = deployment
    if mode == "non_windows":
        monkeypatch.setattr(curl_runtime, "windows_curl_runtime_enabled", lambda: False)
    elif mode == "disabled":
        config.runtime.managed_sandbox.curl_auto_prepare = False
    else:
        candidate = CurlCandidate(Path("installed/curl.exe"), "configured_path" if mode == "explicit" else "service_path")
        monkeypatch.setattr(curl_runtime, "curl_candidates", lambda *args: [candidate])
        if mode == "explicit":
            config.runtime.managed_sandbox.curl_binary = candidate.executable
            probe.return_value = {"code": "backend_incompatible"}
        elif mode == "permission_failure":
            probe.return_value = {"code": "sandbox_process_denied"}
    download = AsyncMock(side_effect=AssertionError("不能下载"))
    monkeypatch.setattr(curl_runtime, "download_archive", download)
    manager = CurlRuntimeManager(lambda: config)
    manager.start()
    await manager.wait()
    expected = {"non_windows": "not_applicable", "disabled": "disabled", "explicit": "unavailable", "permission_failure": "unavailable", "existing": "ready"}
    assert manager.snapshot()["status"] == expected[mode]
    assert not root.exists()
    download.assert_not_called()
    await manager.close()


async def test_two_managers_share_publication_lock(deployment, monkeypatch):
    """独立管理器模拟多个服务实例，锁内复查避免重复下载和目录竞争。"""

    config, root, distribution, data, _ = deployment
    calls = 0

    async def download(root, distribution):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return write_offline(root, distribution, data)

    monkeypatch.setattr(curl_runtime, "download_archive", download)
    managers = [CurlRuntimeManager(lambda: config) for _ in range(2)]
    for manager in managers:
        manager.start()
    await asyncio.wait_for(asyncio.gather(*(manager.wait() for manager in managers)), timeout=5)
    assert calls == 1
    assert all(manager.snapshot()["status"] == "ready" for manager in managers)
    for manager in managers:
        await manager.close()


async def test_service_stop_cancels_download_and_cleans_own_partial(deployment, monkeypatch):
    """真正的下载协程取消后删除 partial，不遗留后台网络任务或可见安装目录。"""

    config, root, _, _, _ = deployment
    started = asyncio.Event()
    stopped = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            try:
                yield b"partial"
                await asyncio.Event().wait()
            finally:
                stopped.set()

    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowStream()))
    monkeypatch.setattr(distribution_module.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, trust_env=False, **kwargs))
    manager = CurlRuntimeManager(lambda: config)
    manager.start()
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.wait_for(manager.close(), timeout=2)
    assert stopped.is_set()
    assert manager.snapshot()["status"] == "cancelled"
    assert not list((root / "downloads").glob(".download-*"))


async def test_background_start_does_not_wait_for_preparation(deployment, monkeypatch):
    """UI 生命周期可立即启动，事件分发只等待首次有界准备，失败后继续。"""

    config, _, _, _, _ = deployment
    manager = ConfigManager(config.config_path)
    runtime = BackgroundRuntime(manager)
    prepared = asyncio.Event()
    scan_started = asyncio.Event()
    dispatched = asyncio.Event()

    async def prepare(config):
        await prepared.wait()
        runtime.curl_runtime._status = {"status": "unavailable", "message": "模拟离线"}

    async def scan(summary):
        scan_started.set()

    async def dispatch(summary):
        dispatched.set()

    monkeypatch.setattr(runtime.curl_runtime, "_prepare", prepare)
    monkeypatch.setattr(runtime._orchestrator, "scan", scan)
    monkeypatch.setattr(runtime._orchestrator, "process_events", dispatch)
    await asyncio.wait_for(runtime.start(), timeout=0.5)
    await asyncio.wait_for(scan_started.wait(), timeout=1)
    assert not dispatched.is_set()
    prepared.set()
    await asyncio.wait_for(dispatched.wait(), timeout=1)
    await runtime.stop()


def test_management_get_is_read_only_and_retry_is_authenticated(deployment, monkeypatch):
    """查询不得安装，重新准备受现有管理 Token 保护，关闭应用完成清理。"""

    config, root, _, _, _ = deployment
    app = create_app(config.config_path, start_scheduler=False)
    manager = app.state.config_manager
    manager.config.web.admin_token_env = "TEST_CURL_ADMIN"
    monkeypatch.setenv("TEST_CURL_ADMIN", "test-secret")
    called = []
    monkeypatch.setattr(app.state.runtime.curl_runtime, "start", lambda **kwargs: called.append(kwargs) or {"status": "preparing"})
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.post("/api/runtime/curl/prepare").status_code == 401
        response = client.get("/api/runtime/curl", headers={"X-Admin-Token": "test-secret"})
        assert response.status_code == 200
        assert response.json()["status"] == "idle"
        assert called == []
        response = client.post("/api/runtime/curl/prepare", headers={"X-Admin-Token": "test-secret"})
        assert response.status_code == 200
        assert called == [{"retry": True}]
    assert not root.exists()


@pytest.fixture
async def native_probe(configured_app_factory, tmp_path, monkeypatch):
    """保留真实启动参数构造，只替换原生进程；CI 不执行测试归档中的假程序。"""

    config = configured_app_factory()
    directory = tmp_path / "中文运行时" / "bin"
    directory.mkdir(parents=True)
    executable = directory / "curl.exe"
    executable.write_bytes(b"test-program")
    ca = directory / "curl-ca-bundle.crt"
    ca.write_bytes(b"test-ca")
    candidate = CurlCandidate(executable, "managed_cache", ca)
    monkeypatch.setattr("teamwork_review_agents.managed_sandbox.inspect_managed_sandbox",
                        lambda *args: SimpleNamespace(available=True, resolved_path="verified-codex.exe"))
    stopped = asyncio.Event()

    class Process:
        """模拟同时读取输出、取消与进程树终止，不创建真实 Windows 控制台。"""

        pid = 123
        returncode = 0

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()

        async def wait(self):
            if self.returncode is None:
                await stopped.wait()
            return self.returncode

    process = Process()
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(curl_runtime.asyncio, "create_subprocess_exec", launch)
    killed = []

    def terminate(pid, **kwargs):
        killed.append((pid, kwargs))
        process.returncode = -9
        stopped.set()

    monkeypatch.setattr(curl_runtime, "terminate_process", terminate)
    return config, candidate, process, launch, killed


async def test_startup_probe_uses_native_curl_without_network_or_secrets(native_probe, monkeypatch):
    """同目录 CA 不产生重复 TOML 键，且外层不经过服务 Python、不发送凭据。"""

    config, candidate, process, launch, killed = native_probe
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    process.stdout.feed_data(b"curl 8.22.0 libcurl/8.22.0 LibreSSL/4.3.2\nProtocols: http https\n")
    process.stdout.feed_eof()
    process.stderr.feed_eof()
    assert await curl_runtime.inspect_installed_curl(candidate, config) == {"code": "ready", "backend": "LibreSSL/4.3.2"}
    command = launch.call_args.args
    environment = launch.call_args.kwargs["env"]
    assert command[0] == "verified-codex.exe"
    assert command[command.index("--") + 1:] == (str(candidate.executable), "-q", "--version")
    profile = tomllib.loads(command[command.index("--config") + 1])["permissions"]["teamwork_curl_probe"]
    assert profile["network"] == {"enabled": False}
    assert profile["filesystem"] == {str(candidate.executable.parent): "read"}
    assert environment["CODEX_HOME"] == str(config.runtime.codex_home)
    assert environment["CURL_SSL_BACKEND"] == "openssl"
    assert "must-not-leak" not in repr(environment)
    assert killed == []


async def test_startup_probe_classifies_execution_denial(native_probe):
    """执行权限错误不是 OpenSSL 缺失，不回显可能含敏感信息的原始 stderr。"""

    config, candidate, process, _, _ = native_probe
    process.returncode = 1
    process.stdout.feed_eof()
    process.stderr.feed_data(b"CreateProcessAsUserW failed: 5 secret-in-error")
    process.stderr.feed_eof()
    result = await curl_runtime.inspect_installed_curl(candidate, config)
    assert result["code"] == "sandbox_process_denied"
    assert "secret-in-error" not in repr(result)


@pytest.mark.parametrize("mode", ["cancel", "output_limit", "timeout"])
async def test_startup_probe_cleans_process_and_pipe_readers(native_probe, monkeypatch, mode):
    """超量输出、超时及服务停止都收尾整个进程树与两个读取协程。"""

    config, candidate, process, launch, killed = native_probe
    process.returncode = None
    before = asyncio.all_tasks()
    if mode == "output_limit":
        process.stdout.feed_data(b"x" * 65537)
    if mode == "timeout":
        monkeypatch.setattr(curl_runtime, "_PROBE_TIMEOUT_SECONDS", 0.02)
    task = asyncio.create_task(curl_runtime.inspect_installed_curl(candidate, config))
    async with asyncio.timeout(2):
        while not launch.called:
            await asyncio.sleep(0)
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif mode == "output_limit":
            with pytest.raises(CurlPreparationError, match="输出异常"):
                await task
        else:
            assert (await task)["code"] == "version_probe_timeout"
    await asyncio.sleep(0)
    assert killed == [(123, {"force": True, "tree": True})]
    assert asyncio.all_tasks() - before == set()


async def test_related_configuration_change_cancels_old_prepare(deployment, monkeypatch):
    """无关配置不重启准备，相关配置变化等待旧任务收尾后才使用新快照。"""

    config, _, _, _, _ = deployment
    manager = CurlRuntimeManager(lambda: config)
    started = asyncio.Event()
    cleaned = asyncio.Event()
    new_started = asyncio.Event()
    calls = 0

    async def prepare(snapshot):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        else:
            assert cleaned.is_set()
            assert snapshot.runtime.managed_sandbox.curl_auto_prepare is False
            new_started.set()

    monkeypatch.setattr(manager, "_prepare", prepare)
    manager.start()
    await asyncio.wait_for(started.wait(), 2)
    old = manager._task
    config.agents["code-reviewer"].prompt = "无关的 Prompt 更新"
    manager.start()
    assert manager._task is old
    config = config.model_copy(deep=True)
    config.runtime.managed_sandbox.curl_auto_prepare = False
    manager.start()
    await asyncio.wait_for(manager.wait(), 2)
    assert new_started.is_set()
    assert old.cancelled()
    await manager.close()


def test_cache_rejects_existing_links(tmp_path):
    """缓存入口不能通过目录链接指向其他位置；Windows 无建链权限时仍测文件冲突。"""

    parent = tmp_path / "runtimes"
    parent.mkdir()
    root = parent / "curl"
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        root.write_bytes(b"not-a-directory")
    with pytest.raises((CurlPreparationError, OSError)):
        ensure_cache_directories(root)
    assert not (outside / "downloads").exists()


async def test_real_windows_managed_curl_prepare(configured_app_factory, monkeypatch):
    """显式联网验收：下载官方包到临时部署缓存，再由真实沙盒直接验证程序可执行。"""

    if os.environ.get("TEAMWORK_TEST_WINDOWS_CURL_PREPARE") != "1":
        pytest.skip("需显式设置 TEAMWORK_TEST_WINDOWS_CURL_PREPARE=1 才下载并验收真实 Windows 分发")
    assert curl_runtime.windows_curl_runtime_enabled(), "真实准备验收必须在 Windows 运行"
    config = configured_app_factory()
    config.runtime.codex_binary = os.environ.get("TEAMWORK_TEST_CODEX_BINARY", "codex")
    config.runtime.codex_home = None
    # 强制覆盖缺少兼容程序的部署场景；原生执行探针保持真实，不调用任何模型。
    monkeypatch.setattr(curl_runtime, "curl_candidates", lambda *args: [])
    manager = CurlRuntimeManager(lambda: config)
    try:
        manager.start()
        await manager.wait()
        assert manager.snapshot()["status"] == "ready", manager.snapshot()
        assert installed_candidate(curl_runtime_root(config)) is not None
        assert manager.snapshot()["source"] == "official_download"
    finally:
        await manager.close()
