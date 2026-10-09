async (page) => {
  // Chrome 手动交互验收使用完整假数据，拦截所有 API，禁止访问真实记录或提交任务。
  const activity = { event_type: "change_request.commits_changed", source: "system" };
  const changes = Array.from({ length: 5 }, (_, index) => ({
    snapshot_key: `request-${index}`, provider: "github-test", repository_id: "demo",
    number: 101 + index, title: `选择测试 ${index + 1}`, state: "opened", draft: false,
    source_branch: `test/${index}`, target_branch: "main", head_sha: "a".repeat(40),
    labels: [], approvals: 0, pipeline_status: "success", merge_status: "clean",
    updated_at: "2026-10-09T01:00:00Z", web_url: `https://example.com/demo/pull/${101 + index}`,
    scanned_at: 1791507600, discovered_event_emitted: true,
    latest_event_checked: true, latest_event_supported: true,
    manual_event: index === 2 ? null : { ...activity }, latest_event: null,
  }));
  const events = changes.map((item, index) => ({
    event_id: `event-${index}`, event_type: activity.event_type, repository_id: "demo",
    number: item.number, status: "failed", attempts: 1, error: "测试失败记录",
    trigger_count: 0, sub_agent_count: 0, agent_queued_count: 0, agent_preparing_count: 0,
    agent_running_count: 0, agent_completed_count: 0, agent_failed_count: 0,
    agent_timed_out_count: 0, agent_cancelled_count: 0, origin: "scanner",
    occurred_at: "2026-10-09T01:00:00Z", created_at: 1791507600, updated_at: 1791507600,
  }));
  const document = {
    repositories: [{ id: "demo", display_name: "测试仓库", provider: "github-test", project: "test/demo", workspace: "./test", enabled: true }],
    providers: { "github-test": { kind: "github", base_url: "https://example.com", token_env: "TEST_TOKEN" } },
    rules: [], scheduled_rules: [], agents: {}, skills: {},
  };
  const status = { paused: false, running_cycle: false, dispatching_events: false, config_revision: "selection-test", stats: { runs: {}, events: { failed: 5 }, change_requests: { total: 5, opened: 5 } } };
  const paginate = (items, url) => ({ items, total: items.length, page: Number(url.searchParams.get("page") || 1), page_size: 10, total_pages: 2 });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() !== "GET") return route.abort("blockedbyclient");
    let body = {};
    if (url.pathname === "/api/config") body = { revision: "selection-test", document };
    else if (url.pathname === "/api/options") body = { events: [activity.event_type] };
    else if (url.pathname === "/api/status") body = status;
    else if (url.pathname === "/api/change-requests") body = paginate(changes, url);
    else if (url.pathname === "/api/events") body = paginate(events, url);
    else if (url.pathname === "/api/runs" || url.pathname === "/api/preflight-runs") body = [];
    else if (url.pathname.startsWith("/api/events/")) body = { ...events.find((item) => url.pathname.endsWith(item.event_id)), dispatches: [], agent_runs: [], preflights: [] };
    else if (url.pathname.startsWith("/api/change-requests/")) body = { ...changes[0], events: [] };
    else if (url.pathname === "/api/codex/runtime-options") body = { models: [], catalog_source: "unavailable", inherited_model: { value: null }, binary: {}, model_cache: {}, user_mcp_servers: [], managed_sandbox: {} };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto("http://127.0.0.1:8080");
  await page.getByRole("link", { name: "#101 选择测试 1 test/0 → main" }).waitFor();
  return { ready: true, requests: changes.length, events: events.length };
}
