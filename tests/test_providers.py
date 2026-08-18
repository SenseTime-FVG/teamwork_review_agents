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


async def test_github_timeline_builds_baseline_then_returns_new_activities() -> None:
    """首次只建立 Timeline 基线，后续按稳定项 ID 返回新增动作。"""

    provider = GitHubProvider(
        "github-main",
        ProviderConfig(
            kind="github",
            base_url="https://api.github.com",
            token_env="GITHUB_TOKEN",
        ),
        ScannerConfig(),
        token="test-token",
    )
    repository = RepositoryConfig(
        id="demo",
        provider="github-main",
        project="owner/demo",
        workspace=Path("/tmp/demo"),
    )
    timeline: list[dict[str, object]] = [
        {
            "event": "committed",
            "node_id": "commit-initial",
            "sha": "a" * 40,
            "author": {"date": "2026-08-17T08:00:00Z"},
        }
    ]

    async def fake_get_json_response(_: str, **__: object):
        return list(timeline), {}

    setattr(provider, "get_json_response", fake_get_json_response)
    try:
        baseline = await provider.list_change_request_activities(repository, 7)
        assert baseline.baseline is True
        assert baseline.activities == ()
        assert baseline.cursor == {"page": 1, "item_id": "commit-initial"}

        timeline.extend(
            [
                {
                    "event": "closed",
                    "id": 101,
                    "created_at": "2026-08-17T08:01:00Z",
                },
                {
                    "event": "reopened",
                    "id": 102,
                    "created_at": "2026-08-17T08:02:00Z",
                },
                {
                    "event": "committed",
                    "node_id": "commit-next",
                    "sha": "b" * 40,
                    "author": {"date": "2026-08-17T08:03:00Z"},
                },
            ]
        )
        incremental = await provider.list_change_request_activities(
            repository,
            7,
            cursor=baseline.cursor,
        )
        assert incremental.baseline is False
        assert [item.type for item in incremental.activities] == [
            "closed",
            "reopened",
            "committed",
        ]
        assert incremental.activities[-1].data["sha"] == "b" * 40

        repeated = await provider.list_change_request_activities(
            repository,
            7,
            cursor=incremental.cursor,
        )
        assert repeated.activities == ()
    finally:
        await provider.close()


async def test_github_timeline_reads_across_page_boundary() -> None:
    """游标位于满页末尾时，下一页活动仍必须被读取。"""

    provider = GitHubProvider(
        "github-main",
        ProviderConfig(
            kind="github",
            base_url="https://api.github.com",
            token_env="GITHUB_TOKEN",
        ),
        ScannerConfig(),
        token="test-token",
    )
    repository = RepositoryConfig(
        id="demo",
        provider="github-main",
        project="owner/demo",
        workspace=Path("/tmp/demo"),
    )
    first_page = [
        {
            "event": "commented",
            "id": index,
            "created_at": "2026-08-17T08:00:00Z",
        }
        for index in range(1, 101)
    ]
    second_page = [
        {
            "event": "closed",
            "id": 101,
            "created_at": "2026-08-17T08:01:00Z",
        },
        {
            "event": "reopened",
            "id": 102,
            "created_at": "2026-08-17T08:02:00Z",
        },
    ]

    async def fake_get_json_response(_: str, **__: object):
        return first_page, {}

    async def fake_get_json(_: str, **kwargs: object):
        params = kwargs["params"]
        assert isinstance(params, dict)
        return second_page if params["page"] == 2 else []

    setattr(provider, "get_json_response", fake_get_json_response)
    setattr(provider, "get_json", fake_get_json)
    try:
        result = await provider.list_change_request_activities(
            repository,
            7,
            cursor={"page": 1, "item_id": "100"},
        )
    finally:
        await provider.close()

    assert [item.type for item in result.activities] == ["closed", "reopened"]
    assert result.cursor == {"page": 2, "item_id": "102"}


async def test_github_timeline_first_scan_only_replays_requested_window() -> None:
    """首次游标应保留最新位置，同时只返回回看时间窗内的活动。"""

    provider = GitHubProvider(
        "github-main",
        ProviderConfig(
            kind="github",
            base_url="https://api.github.com",
            token_env="GITHUB_TOKEN",
        ),
        ScannerConfig(),
        token="test-token",
    )
    repository = RepositoryConfig(
        id="demo",
        provider="github-main",
        project="owner/demo",
        workspace=Path("/tmp/demo"),
    )
    timeline = [
        {
            "event": "closed",
            "id": 100,
            "created_at": "2026-08-17T07:59:00Z",
        },
        {
            "event": "reopened",
            "id": 101,
            "created_at": "2026-08-17T08:01:00Z",
        },
        {
            "event": "labeled",
            "id": 102,
            "created_at": "2026-08-17T08:02:00Z",
            "label": {"name": "ready"},
        },
    ]

    async def fake_get_json_response(_: str, **__: object):
        return timeline, {}

    setattr(provider, "get_json_response", fake_get_json_response)
    try:
        result = await provider.list_change_request_activities(
            repository,
            7,
            since=datetime(2026, 8, 17, 8, tzinfo=UTC),
        )
    finally:
        await provider.close()

    assert result.baseline is False
    assert [item.type for item in result.activities] == ["reopened", "labeled"]
    assert result.cursor == {"page": 1, "item_id": "102"}


async def test_github_timeline_first_window_reads_back_across_pages() -> None:
    """首次窗口跨页时应从末页向前读到边界，再保持平台顺序。"""

    provider = GitHubProvider(
        "github-main",
        ProviderConfig(
            kind="github",
            base_url="https://api.github.com",
            token_env="GITHUB_TOKEN",
        ),
        ScannerConfig(),
        token="test-token",
    )
    provider.TIMELINE_PAGE_SIZE = 2
    repository = RepositoryConfig(
        id="demo",
        provider="github-main",
        project="owner/demo",
        workspace=Path("/tmp/demo"),
    )
    pages = {
        1: [
            {"event": "closed", "id": 1, "created_at": "2026-08-17T07:55:00Z"},
            {"event": "reopened", "id": 2, "created_at": "2026-08-17T07:56:00Z"},
        ],
        2: [
            {"event": "closed", "id": 3, "created_at": "2026-08-17T07:59:00Z"},
            {"event": "reopened", "id": 4, "created_at": "2026-08-17T08:00:00Z"},
        ],
        3: [
            {"event": "closed", "id": 5, "created_at": "2026-08-17T08:01:00Z"},
            {"event": "reopened", "id": 6, "created_at": "2026-08-17T08:02:00Z"},
        ],
    }
    fetched_pages: list[int] = []

    async def fake_get_json_response(_: str, **__: object):
        return pages[1], {"Link": '<https://api.github.com?page=3>; rel="last"'}

    async def fake_get_json(_: str, **kwargs: object):
        params = kwargs["params"]
        assert isinstance(params, dict)
        page = int(params["page"])
        fetched_pages.append(page)
        return pages[page]

    setattr(provider, "get_json_response", fake_get_json_response)
    setattr(provider, "get_json", fake_get_json)
    try:
        result = await provider.list_change_request_activities(
            repository,
            7,
            since=datetime(2026, 8, 17, 8, tzinfo=UTC),
        )
    finally:
        await provider.close()

    assert fetched_pages == [3, 2]
    assert [item.id for item in result.activities] == ["4", "5", "6"]
    assert result.cursor == {"page": 3, "item_id": "6"}
