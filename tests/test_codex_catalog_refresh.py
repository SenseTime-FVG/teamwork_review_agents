"""当前 CLI 模型目录、失败降级与生命周期回归，不访问真实账号。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from teamwork_review_agents import codex_account, codex_settings
from teamwork_review_agents.codex_account import CodexAccountError, read_codex_runtime_snapshot
from teamwork_review_agents.config import CodexRuntimeConfig


class FakeServer:
    """仅提供只读配置和目录接口，记录分页与关闭行为。"""

    def __init__(self, pages, config=None, start_error=None):
        self.pages = iter(pages)
        self.config = config if config is not None else {"model": "selected-model"}
        self.start_error = start_error
        self.calls = []
        self.closed = False

    async def start(self):
        if self.start_error:
            raise self.start_error

    async def close(self):
        self.closed = True

    async def request(self, method, params):
        self.calls.append((method, params))
        value = {"config": self.config} if method == "config/read" else next(self.pages)
        if method == "config/read" and isinstance(self.config, BaseException):
            raise self.config
        if isinstance(value, BaseException):
            raise value
        return value


def install_server(monkeypatch, server):
    """捕获实际启动参数，验证不会使用临时 Agent Home 或另一份 CLI。"""

    starts = []

    def create(binary, home, **kwargs):
        starts.append((binary, home, kwargs))
        return server

    monkeypatch.setattr(codex_account, "CodexAppServer", create)
    return starts


async def test_snapshot_uses_one_server_and_paginates(monkeypatch, tmp_path):
    server = FakeServer([
        {"data": [{"model": "new-a"}, {"model": "hidden", "hidden": True}], "nextCursor": "next"},
        {"data": [{"model": "new-b"}], "nextCursor": None},
    ])
    starts = install_server(monkeypatch, server)
    result = await read_codex_runtime_snapshot("/saved/codex", tmp_path)
    assert result.config == {"model": "selected-model"}
    assert result.models == [{"model": "new-a"}, {"model": "new-b"}]
    assert result.models_error is None
    assert starts == [("/saved/codex", tmp_path, {"working_directory": tmp_path})]
    assert server.calls == [
        ("config/read", {"includeLayers": False}),
        ("model/list", {"limit": 100, "includeHidden": False}),
        ("model/list", {"limit": 100, "includeHidden": False, "cursor": "next"}),
    ]
    assert server.closed


@pytest.mark.parametrize("last_page", [
    {"data": [{"model": "b"}], "nextCursor": "next"},
    {"data": [], "nextCursor": 123},
    {"data": "malformed"},
    {"data": [{}]},
    {"data": [{"model": " "}]},
    CodexAccountError("原始错误含敏感内容"),
    TimeoutError("原始错误含敏感内容"),
])
async def test_incomplete_catalog_is_not_published(monkeypatch, tmp_path, last_page):
    server = FakeServer([{"data": [{"model": "a"}], "nextCursor": "next"}, last_page])
    install_server(monkeypatch, server)
    result = await read_codex_runtime_snapshot("codex", tmp_path)
    assert result.models is None
    assert result.models_error
    assert "原始错误" not in str(result)
    assert result.config == {"model": "selected-model"}
    assert server.closed


async def test_model_list_survives_config_failure(monkeypatch, tmp_path):
    server = FakeServer([{"data": []}], config=CodexAccountError("秘密"))
    install_server(monkeypatch, server)
    result = await read_codex_runtime_snapshot("codex", tmp_path)
    assert result.config is None and result.config_error
    assert result.models == [] and result.models_error is None
    assert "秘密" not in str(result)
    assert server.closed


async def test_pagination_is_bounded(monkeypatch, tmp_path):
    server = FakeServer([{"data": [], "nextCursor": str(i)} for i in range(50)])
    install_server(monkeypatch, server)
    result = await read_codex_runtime_snapshot("codex", tmp_path)
    assert result.models is None and result.models_error
    assert len(server.calls) == 51
    assert server.closed


async def test_total_model_timeout_cleans_up(monkeypatch, tmp_path):
    server = FakeServer([])
    original_request = server.request

    async def request(method, params):
        if method == "model/list":
            await asyncio.Event().wait()
        return await original_request(method, params)

    server.request = request
    install_server(monkeypatch, server)
    monkeypatch.setattr(codex_account, "MODEL_LIST_TIMEOUT_SECONDS", 0.01)
    result = await read_codex_runtime_snapshot("codex", tmp_path)
    assert result.models is None and result.models_error
    assert result.config == {"model": "selected-model"}
    assert server.closed


@pytest.mark.parametrize("error", [OSError("私密路径"), asyncio.CancelledError()])
async def test_start_failure_or_cancellation_closes_server(monkeypatch, tmp_path, error):
    server = FakeServer([], start_error=error)
    install_server(monkeypatch, server)
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await read_codex_runtime_snapshot("codex", tmp_path)
    else:
        result = await read_codex_runtime_snapshot("codex", tmp_path)
        assert result.models is None and result.config_error and result.models_error
        assert "私密路径" not in str(result)
    assert server.closed


@pytest.fixture
def isolated_runtime(monkeypatch):
    """隔离 CLI 子进程诊断，确保测试不碰真实安装或沙盒。"""

    monkeypatch.setattr(codex_settings, "inspect_codex_binary", lambda *_: {"version": "new"})
    monkeypatch.setattr(codex_settings, "inspect_managed_sandbox", lambda *_: SimpleNamespace(as_dict=lambda: {}))


@pytest.mark.parametrize("models", [[], [
    {"model": "new", "displayName": "New", "defaultReasoningEffort": "medium",
     "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}],
     "serviceTiers": [{"id": "priority", "name": "Fast"}], "private": "不能返回"},
    {"model": "new"}, {"model": "hidden", "hidden": True},
]])
def test_live_directory_wins_even_if_empty(tmp_path, monkeypatch, isolated_runtime, models):
    (tmp_path / "models_cache.json").write_text(json.dumps({
        "client_version": "old", "fetched_at": "2020-01-01T00:00:00Z",
        "models": [{"slug": "old", "visibility": "list"}],
    }), encoding="utf-8")
    monkeypatch.setattr(codex_settings, "read_bundled_models", lambda *_: pytest.fail("不能读取内置目录"))
    result = codex_settings.inspect_runtime_options(
        CodexRuntimeConfig(model="keep-selected"), "codex", tmp_path,
        live_models=models,
    )
    assert result["catalog_source"] == "app_server"
    assert result["catalog_error"] is None and result["catalog_warning"] is None
    assert result["inherited_model"]["value"] == "keep-selected"
    assert result["catalog_checked_at"]
    assert "不能返回" not in json.dumps(result, ensure_ascii=False)
    assert result["models"] == ([{
        "slug": "new", "display_name": "New", "default_reasoning_level": "medium",
        "supported_reasoning_levels": ["low", "high"], "supports_fast_mode": True,
    }] if models else [])
    assert "未直接采用旧缓存" in result["version_warning"]


@pytest.mark.parametrize("cache, bundled, source", [
    (True, False, "account_cache"), (False, True, "bundled"), (False, False, "unavailable"),
])
def test_failures_have_explicit_fallback(tmp_path, monkeypatch, isolated_runtime, cache, bundled, source):
    if cache:
        (tmp_path / "models_cache.json").write_text(
            '{"models":[{"slug":"old","visibility":"list"}]}', encoding="utf-8",
        )
    monkeypatch.setattr(codex_settings, "read_bundled_models", lambda *_: (
        ([{"slug": "built-in"}], None) if bundled else ([], "不能返回的子进程原文")
    ))
    result = codex_settings.inspect_runtime_options(
        CodexRuntimeConfig(), "codex", tmp_path, live_models_error="查询失败",
    )
    assert result["catalog_source"] == source
    assert result["catalog_error"] == "查询失败"
    assert result["catalog_warning"]
    assert "不能返回" not in json.dumps(result, ensure_ascii=False)
