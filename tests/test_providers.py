"""代码托管平台自动分页与增量扫描测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from teamwork_review_agents.config import ProviderConfig, RepositoryConfig, ScannerConfig
from teamwork_review_agents.providers.github import GitHubProvider


async def test_github_provider_auto_pages_and_honors_item_limit(snapshot_factory) -> None:
    """Provider 应自动翻页，但不能超过单仓库单轮数量上限。"""

    provider = GitHubProvider(
        "github-main",
        ProviderConfig(
            kind="github",
            base_url="https://api.github.com",
            token_env="GITHUB_TOKEN",
        ),
        ScannerConfig(max_items_per_repository=3, api_page_size=2),
        token="test-token",
    )
    repository = RepositoryConfig(
        id="demo",
        provider="github-main",
        project="git@github.com:owner/demo.git",
        workspace=Path("/tmp/demo"),
    )
    calls: list[dict[str, int]] = []
    pages = {
        1: [
            {"number": 1, "updated_at": "2026-08-17T12:00:00Z"},
            {"number": 2, "updated_at": "2026-08-17T11:00:00Z"},
        ],
        2: [
            {"number": 3, "updated_at": "2026-08-17T10:00:00Z"},
        ],
    }

    async def fake_get_json(_: str, **kwargs: object) -> object:
        params = kwargs["params"]
        assert isinstance(params, dict)
        calls.append(params)
        return pages[int(params["page"])]

    async def fake_build_snapshot(
        _: RepositoryConfig,
        item: dict[str, object],
    ):
        return snapshot_factory(
            provider="github-main",
            repository_id="demo",
            number=int(item["number"]),
        )

    setattr(provider, "get_json", fake_get_json)
    setattr(provider, "_build_snapshot", fake_build_snapshot)
    try:
        snapshots = await provider.list_change_requests(repository)
    finally:
        await provider.close()

    assert [snapshot.number for snapshot in snapshots] == [1, 2, 3]
    assert [call["per_page"] for call in calls] == [2, 1]
    assert repository.project == "owner/demo"


async def test_github_provider_stops_at_previous_scan_watermark(snapshot_factory) -> None:
    """读取到上次扫描时间之前的项目后应停止继续翻页。"""

    provider = GitHubProvider(
        "github-main",
        ProviderConfig(
            kind="github",
            base_url="https://api.github.com",
            token_env="GITHUB_TOKEN",
        ),
        ScannerConfig(max_items_per_repository=10, api_page_size=3),
        token="test-token",
    )
    repository = RepositoryConfig(
        id="demo",
        provider="github-main",
        project="owner/demo",
        workspace=Path("/tmp/demo"),
    )
    call_count = 0

    async def fake_get_json(_: str, **__: object) -> object:
        nonlocal call_count
        call_count += 1
        return [
            {"number": 1, "updated_at": "2026-08-17T12:00:00Z"},
            {"number": 2, "updated_at": "2026-08-17T09:00:00Z"},
            {"number": 3, "updated_at": "2026-08-17T08:00:00Z"},
        ]

    async def fake_build_snapshot(
        _: RepositoryConfig,
        item: dict[str, object],
    ):
        return snapshot_factory(
            provider="github-main",
            repository_id="demo",
            number=int(item["number"]),
        )

    setattr(provider, "get_json", fake_get_json)
    setattr(provider, "_build_snapshot", fake_build_snapshot)
    try:
        snapshots = await provider.list_change_requests(
            repository,
            updated_since=datetime(2026, 8, 17, 10, tzinfo=UTC),
        )
    finally:
        await provider.close()

    assert [snapshot.number for snapshot in snapshots] == [1]
    assert call_count == 1
