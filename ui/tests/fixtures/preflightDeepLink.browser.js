async (page) => {
  // 无界面 Chrome 验收：仅服务静态构建，所有 API 都被拦截，不读取真实配置或提交任务。
  const runId = "11111111-2222-4333-8444-555555555555";
  const otherRunId = "66666666-7777-4888-8999-000000000000";
  const document = {
    repositories: [], providers: {}, rules: [], scheduled_rules: [], agents: {}, skills: {},
  };
  const status = {
    paused: false, running_cycle: false, dispatching_events: false, config_revision: "link-fixture",
    stats: { runs: {}, events: {}, change_requests: {} },
  };
  const detail = (id) => ({
    run_id: id, repository_id: "fixture", number: 83, head_sha: "a".repeat(40),
    config_revision: "link-fixture", source_generation: 1, phase: "finished", status: "success",
    attempts: 1, status_published: true, trigger_source: "event", branch: "feature/fixture",
    started_at: 1792489610, finished_at: 1792489832, failed_step: null, error: null,
    output: `详情链接验收输出 ${id}`, steps: [], linked_events: [],
  });
  await page.addInitScript(() => {
    // 每个新页面先走认证失败，确保链接本身不携带凭据或绕过认证。
    localStorage.clear();
    window.__preflightLinkTest = { requests: [], mutations: [] };
  });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    await page.evaluate(({ path, method }) => {
      window.__preflightLinkTest.requests.push(path);
      if (method !== "GET") window.__preflightLinkTest.mutations.push({ path, method });
    }, { path: url.pathname, method: request.method() });
    if (request.method() !== "GET") return route.abort("blockedbyclient");
    if (request.headers()["authorization"] !== "Bearer fixture-only-token") {
      return route.fulfill({ status: 401, json: { detail: "管理员认证失败" } });
    }
    if (url.pathname.endsWith("/stream")) {
      return route.fulfill({ contentType: "text/event-stream", body: "event: end\ndata: {}\n\n" });
    }
    let body = {};
    let code = 200;
    if (url.pathname === "/api/config") body = { revision: "link-fixture", document };
    else if (url.pathname === "/api/status") body = status;
    else if (url.pathname === "/api/options") body = { events: [] };
    else if (["/api/runs", "/api/preflight-runs"].includes(url.pathname)) body = [];
    else if ([runId, otherRunId].some((id) => url.pathname === `/api/preflight-runs/${id}`)) body = detail(url.pathname.split("/").at(-1));
    else if (url.pathname.startsWith("/api/preflight-runs/")) { body = { detail: "本地 CI 运行记录不存在" }; code = 404; }
    else if (["/api/events", "/api/change-requests"].includes(url.pathname)) body = { items: [], total: 0, page: 1, page_size: 10, total_pages: 1 };
    else if (url.pathname === "/api/codex/runtime-options") body = {
      models: [], catalog_source: "unavailable", inherited_model: { value: null },
      binary: {}, model_cache: {}, user_mcp_servers: [], managed_sandbox: {},
      inherited_settings: {},
    };
    else if (url.pathname === "/api/config/versions") body = [];
    return route.fulfill({ status: code, json: body });
  });
  // 重复运行时强制重新加载文档，不能只触发当前页面的 hashchange。
  await page.goto("about:blank");
  await page.goto(`http://127.0.0.1:18765/#preflight=${runId}`);
  await page.getByText("管理员认证失败", { exact: true }).waitFor();
  return { ready: true, runId, otherRunId, expectedToken: "fixture-only-token" };
}
