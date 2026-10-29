"""请求级超时、真实响应续期和取消边界；不访问网络或真实账号。"""

import asyncio

import httpx
import pytest

from teamwork_review_agents.model_request import ModelRequestTimeout, record_model_transport, run_model_request
from teamwork_review_agents.run_control import RunControl, active_run_control


async def _ignore(*args):
    """接受测试用事件，不做额外 IO。"""


async def _request(request, **options):
    """给测试提供短时限默认值。"""

    return await run_model_request(request, callback=_ignore, emit=_ignore,
                                   idle_seconds=options.pop("idle_seconds", 0.05),
                                   total_seconds=options.pop("total_seconds", 1), **options)


@pytest.mark.asyncio
async def test_silent_request_times_out_without_stopping_agent():
    """单次静默请求必须被取消，运行控制仍可继续执行备用请求。"""

    cancelled = asyncio.Event()
    control = RunControl("test")
    token = active_run_control.set(control)

    async def silent(receive):
        try:
            await record_model_transport("awaiting_headers")
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    try:
        with pytest.raises(ModelRequestTimeout) as caught:
            await _request(silent)
        assert caught.value.details["timeout_kind"] == "idle"
        assert caught.value.details["stage"] == "awaiting_headers"
        assert cancelled.is_set()
        assert control.stop is None
        assert control.runtime_wait_deadline is None
        assert control.model_request_deadline is None
    finally:
        active_run_control.reset(token)


@pytest.mark.asyncio
async def test_provider_queue_is_bounded_and_does_not_send_request():
    """并发名额等待超时不会泄漏信号量或调用上游。"""

    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    called = False

    async def request(receive):
        nonlocal called
        called = True

    with pytest.raises(ModelRequestTimeout) as caught:
        await _request(request, semaphore=semaphore)
    assert caught.value.details["stage"] == "provider_queue"
    assert not called
    semaphore.release()
    async with asyncio.timeout(0.1):
        await semaphore.acquire()


@pytest.mark.asyncio
async def test_real_response_progress_renews_idle_but_not_total():
    """持续响应可以超过 idle，总请求期限仍不可由事件无限续期。"""

    async def streaming(receive):
        for _ in range(8):
            await receive({"type": "response.output_text.delta", "delta": "测试"})
            await asyncio.sleep(0.02)
        return {"output": []}

    assert await _request(streaming) == {"output": []}
    with pytest.raises(ModelRequestTimeout) as caught:
        await _request(streaming, total_seconds=0.1)
    assert caught.value.details["timeout_kind"] == "total"
    assert caught.value.details["response_event_count"] > 0


@pytest.mark.asyncio
async def test_diagnostic_heartbeat_does_not_renew_idle():
    """连接重试诊断不是模型进展，不能无限保活。"""

    async def waiting(receive):
        while True:
            await record_model_transport("transport_backoff")
            await asyncio.sleep(0.01)

    with pytest.raises(ModelRequestTimeout) as caught:
        await _request(waiting)
    assert caught.value.details["timeout_kind"] == "idle"
    assert caught.value.details["response_event_count"] == 0


@pytest.mark.asyncio
async def test_cancel_does_not_become_retryable_timeout():
    """外部取消原样传播，不能误触发超时重试。"""

    entered = asyncio.Event()

    async def request(receive):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_request(request))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_swallowed_timeout_cannot_return_success():
    """即使客户端吞掉超时取消，过期响应也不可算成功。"""

    async def request(receive):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"output": []}

    control = RunControl("test")
    token = active_run_control.set(control)
    try:
        with pytest.raises(ModelRequestTimeout):
            await _request(request)
        assert control.stop is None
    finally:
        active_run_control.reset(token)


@pytest.mark.asyncio
async def test_streaming_wait_does_not_move_tool_idle_clock_into_future():
    """响应续期后退出等待，后续工具阶段仍从当前时间计算 idle。"""

    control = RunControl("test")
    token = active_run_control.set(control)

    async def request(receive):
        for _ in range(5):
            control.progress()
            await receive({"type": "response.created"})
            await asyncio.sleep(0.02)
        return {"output": []}

    try:
        await _request(request)
        assert control.last_progress_at <= asyncio.get_running_loop().time()
        assert control.model_request_deadline is None
    finally:
        active_run_control.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["codex", "external"])
async def test_http_phase_and_request_id_survive_stalled_body(kind, monkeypatch, mock_codex_client_version):
    """区分已收到响应头后的正文静默，记录请求 ID 而不记录认证或正文。"""

    from teamwork_review_agents.codex_model_client import CodexResponsesClient
    from teamwork_review_agents.config import ModelProviderConfig
    from teamwork_review_agents.model_provider_client import ExternalModelClient

    closed = asyncio.Event()
    logs = []

    class Stream(httpx.AsyncByteStream):
        """用不结束的响应体模拟上游连接卡住。"""

        async def __aiter__(self):
            if kind == "codex":
                yield b'data: {"type":"response.created","response":{"id":"test"}}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    class OAuth:
        """不读取真实凭据，认证头由测试替身提供。"""

        async def credentials(self):
            return object()

    async def handler(request):
        return httpx.Response(200, headers={"x-request-id": "req-unit", "content-type": "text/event-stream" if kind == "codex" else "application/json"}, stream=Stream())

    async def emit(event, data):
        logs.append((event, data))

    transport = httpx.MockTransport(handler)
    if kind == "codex":
        monkeypatch.setattr("teamwork_review_agents.codex_model_client._codex_headers", lambda *args, **kwargs: {"Authorization": "Bearer test-secret"})
        client = CodexResponsesClient(oauth=OAuth(), codex_binary="test", transport=transport)
    else:
        client = ExternalModelClient(ModelProviderConfig(display_name="test", driver="openai_responses", base_url="https://unit.test", default_model="test"), "test-secret", timeout_seconds=1, idle_timeout_seconds=1, transport=transport)
    with pytest.raises(ModelRequestTimeout) as caught:
        await run_model_request(lambda receive: client.create_response({"model": "test"}, event_callback=receive),
                                callback=_ignore, emit=emit, idle_seconds=0.1, total_seconds=2)
    assert caught.value.details["http_status"] == 200
    assert caught.value.details["upstream_request_id"] == "req-unit"
    assert caught.value.details["stage"] == ("receiving_response" if kind == "codex" else "reading_response")
    assert closed.is_set()
    assert "test-secret" not in str(logs)
