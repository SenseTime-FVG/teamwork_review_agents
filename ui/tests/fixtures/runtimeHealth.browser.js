async (page) => {
  // 仅访问静态构建并拦截全部 API，模拟数据库故障，不读取真实数据或提交任务。
  let mode = "retrying";
  let releaseLists;
  const blockedLists = new Promise((resolve) => { releaseLists = resolve; });
  const error = "OperationalError: unable to open database file；SQLite SQLITE_CANTOPEN（14）";
  const config = { repositories: [], providers: {}, rules: [], scheduled_rules: [], agents: {}, skills: {} };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET") return route.abort("blockedbyclient");
    if (mode !== "healthy" && ["/api/runs", "/api/preflight-runs", "/api/events", "/api/change-requests"].includes(path)) {
      // 首屏执行列表保持等待，确认其不会阻塞配置和故障状态的展示。
      if (["/api/runs", "/api/preflight-runs"].includes(path) && mode === "retrying") await blockedLists;
      return route.fulfill({ status: 500, json: { detail: "模拟数据库不可用" } });
    }
    let body = {};
    if (path === "/api/config") body = { revision: "health-fixture", document: config };
    else if (path === "/api/options") body = { events: [] };
    else if (path === "/api/status") body = {
      paused: false, running_cycle: mode !== "healthy", dispatching_events: false,
      config_revision: "health-fixture", stats_available: mode === "healthy",
      status_error: mode === "healthy" ? null : error,
      last_error: mode === "healthy" ? null : error,
      background_tasks: { scanner: mode === "healthy" ? { state: "running" } : {
        state: mode, error, failed_at: 1792888952, retry_at: mode === "retrying" ? 1792889012 : null,
      } },
      stats: { runs: {}, events: {}, change_requests: {} },
    };
    else if (["/api/runs", "/api/preflight-runs"].includes(path)) body = [];
    else if (["/api/events", "/api/change-requests"].includes(path)) body = { items: [], total: 0, page: 1, page_size: 10, total_pages: 1 };
    else if (path === "/api/codex/runtime-options") body = {
      models: [], catalog_source: "unavailable", inherited_model: { value: null },
      binary: {}, model_cache: {}, user_mcp_servers: [], managed_sandbox: {}, inherited_settings: {},
    };
    return route.fulfill({ status: 200, json: body });
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("http://127.0.0.1:18765");
  await page.getByRole("heading", { name: "后台运行异常", exact: true }).waitFor();
  await page.getByText(/计划自动重试/).waitFor();
  if (await page.locator(".metric-grid").count()) throw new Error("统计不可用时不能显示为零的指标");
  if (!await page.locator(".sidebar-footer .service-dot.error").count()) throw new Error("侧栏必须同步显示错误");
  releaseLists();
  await page.screenshot({ path: "output/playwright/runtime-recovery.png", fullPage: true });

  mode = "recovering";
  await page.getByText(/正在尝试恢复，成功后解除告警/).waitFor();
  if (await page.locator(".hero-card h1").innerText() !== "后台运行异常") throw new Error("重试开始不能提前消除故障");
  mode = "stopped";
  await page.getByText(/正在尝试恢复，成功后解除告警/).waitFor({ state: "detached" });
  if (await page.locator(".hero-card h1").innerText() !== "后台运行异常") throw new Error("任务退出不能显示为扫描中");

  mode = "healthy";
  await page.getByRole("heading", { name: "服务运行正常", exact: true }).waitFor();
  await page.locator(".metric-grid").waitFor();
  if (await page.locator(".hero-card.unhealthy").count()) throw new Error("恢复成功后应解除主状态告警");
  if (await page.locator(".sidebar-footer .service-dot.error").count()) throw new Error("恢复成功后应解除侧栏告警");
  return { passed: true, cases: ["数据库故障冷启动", "列表失败不遮住健康信息", "自动重试时间", "恢复中保留告警", "任务退出覆盖残留扫描中", "成功后解除告警"] };
}
