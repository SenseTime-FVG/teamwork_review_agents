"""只读平台请求在断流、限流和取消时的回归测试。"""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from teamwork_review_agents.config import ProviderConfig, ScannerConfig
from teamwork_review_agents.providers.base import ProviderError
from teamwork_review_agents.providers.github import GitHubProvider
from teamwork_review_agents.providers.gitlab import GitLabProvider


@pytest.fixture(params=[GitHubProvider, GitLabProvider])
async def provider(request):
    """两个平台共用相同的只读重试边界。"""

    kind = "github" if request.param is GitHubProvider else "gitlab"
    instance = request.param(
        f"{kind}-test",
        ProviderConfig(kind=kind, base_url="https://platform.test", token_env="TEST_TOKEN"),
        ScannerConfig(),
        token="private-test-token",
    )
    await instance.client.aclose()
    yield instance
    await instance.close()


@pytest.fixture
def delays(monkeypatch):
    """记录退避时长，让故障测试不必真实等待。"""

    values = []

    async def record_sleep(seconds):
        values.append(seconds)

    monkeypatch.setattr("teamwork_review_agents.providers.base.sleep", record_sleep)
    return values


def install_transport(provider, handler):
    """替换为内存传输，禁止测试访问真实平台。"""

    provider.client = httpx.AsyncClient(
        base_url="https://platform.test", headers=provider.headers(),
        transport=httpx.MockTransport(handler),
    )


async def test_partial_body_is_discarded_before_retry(provider, delays, caplog):
    """复现响应体中途断流，成功重试后只返回完整 JSON 及其分页头。"""

    class BrokenBody(httpx.AsyncByteStream):
        """模拟收到部分 JSON 后连接被远端提前关闭。"""

        closed = False

        async def __aiter__(self):
            yield b'{"partial":'
            raise httpx.RemoteProtocolError("incomplete chunked read")

        async def aclose(self):
            self.closed = True

    body = BrokenBody()
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, stream=body)
        return httpx.Response(200, json={"complete": True}, headers={"X-Next-Page": "3"})

    install_transport(provider, handler)
    result, headers = await provider.get_json_response(
        "/items?access_token=query-secret", params={"page": 2},
    )

    assert result == {"complete": True}
    assert headers["x-next-page"] == "3"
    assert body.closed
    assert len(requests) == 2
    assert requests[0].url == requests[1].url
    assert requests[1].url.params["page"] == "2"
    assert delays == [1.0]
    assert "RemoteProtocolError" in caplog.text
    assert "/items" in caplog.text
    assert "query-secret" not in caplog.text
    assert "private-test-token" not in caplog.text


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
async def test_transport_failure_recovers_with_bounded_backoff(provider, delays, error):
    """连接失败、超时和断流按一秒、两秒退避，第三次成功。"""

    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            raise error("短暂传输故障", request=request)
        return httpx.Response(200, json=["ok"])

    install_transport(provider, handler)
    assert await provider.get_json("/items") == ["ok"]
    assert len(calls) == 3
    assert delays == [1.0, 2.0]


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_transient_http_status_recovers_for_optional_get(provider, delays, status):
    """可选信息接口也应重试繁忙或限流，且不能误用失败响应正文。"""

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status if len(calls) == 1 else 200, json={"ok": True})

    install_transport(provider, handler)
    assert await provider.get_optional_json("/items", []) == {"ok": True}
    assert len(calls) == 2
    assert delays == [1.0]


@pytest.mark.parametrize("optional", [False, True])
@pytest.mark.parametrize("failure", ["disconnect", 503])
async def test_exhausted_get_still_reports_error(provider, delays, optional, failure):
    """耗尽后保留原始失败原因，让扫描层继续报告异常。"""

    calls = []

    def handler(request):
        calls.append(request)
        if failure == "disconnect":
            raise httpx.RemoteProtocolError("incomplete chunked read", request=request)
        return httpx.Response(failure)

    install_transport(provider, handler)
    with pytest.raises(ProviderError) as caught:
        if optional:
            await provider.get_optional_json("/items", [])
        else:
            await provider.get_json("/items")
    expected_error = "incomplete chunked read" if failure == "disconnect" else "503"
    assert expected_error in str(caught.value)
    assert len(calls) == 3
    assert delays == [1.0, 2.0]


@pytest.mark.parametrize("optional", [False, True])
@pytest.mark.parametrize("status", [401, 403, 404, 422, 501])
async def test_permanent_errors_are_not_retried(provider, delays, optional, status):
    """不重试认证或参数错误，保持可选接口的权限不足与缺失回退。"""

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    install_transport(provider, handler)
    fallback = object()
    if optional and status in {403, 404}:
        assert await provider.get_optional_json("/items", fallback) is fallback
    else:
        with pytest.raises(ProviderError, match=str(status)):
            if optional:
                await provider.get_optional_json("/items", fallback)
            else:
                await provider.get_json("/items")
    assert len(calls) == 1
    assert delays == []


async def test_invalid_json_is_not_retried(provider, delays):
    """完整传输的非法 JSON 属于数据错误，不能被瞬态故障重试掩盖。"""

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text="invalid json")

    install_transport(provider, handler)
    with pytest.raises(ProviderError):
        await provider.get_json("/items")
    assert len(calls) == 1
    assert delays == []


@pytest.mark.parametrize("retry_after,expected", [
    ("7", 7.0), ("99999", 30.0), ("bad", 1.0), ("-1", 1.0),
    ("inf", 1.0), ("nan", 1.0), ("Thu, 05 Nov 2026 01:00:07 GMT", 7.0),
])
async def test_retry_after_is_bounded(provider, delays, monkeypatch, retry_after, expected):
    """秒数和日期均能生效，异常或超长等待不会导致无限退避。"""

    class Clock(datetime):
        """固定日期格式的 Retry-After 计算基准。"""

        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 5, 1, tzinfo=UTC)

    monkeypatch.setattr("teamwork_review_agents.providers.base.datetime", Clock)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json=[])

    install_transport(provider, handler)
    assert await provider.get_json("/items") == []
    assert delays == [expected]


async def test_cancellation_interrupts_backoff(provider, monkeypatch):
    """外层取消能立即结束退避，不继续请求或转换成普通平台失败。"""

    sleeping = asyncio.Event()
    calls = []

    async def wait_until_cancelled(seconds):
        sleeping.set()
        await asyncio.Event().wait()

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    monkeypatch.setattr("teamwork_review_agents.providers.base.sleep", wait_until_cancelled)
    install_transport(provider, handler)
    task = asyncio.create_task(provider.get_json("/items"))
    try:
        await asyncio.wait_for(sleeping.wait(), 1)
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1


@pytest.mark.parametrize("method", ["post_json", "put_optional_json", "patch_optional_json", "delete_resource"])
async def test_writes_are_not_retried(provider, delays, method):
    """写操作即使遇到服务端错误，也不能自动重放副作用。"""

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    install_transport(provider, handler)
    with pytest.raises(ProviderError, match="503"):
        if method == "delete_resource":
            await provider.delete_resource("/items/1")
        else:
            await getattr(provider, method)("/items/1", {"body": "测试"})
    assert len(calls) == 1
    assert delays == []
