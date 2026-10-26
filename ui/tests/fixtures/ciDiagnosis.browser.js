async (page) => {
  // 只读界面验收使用静态服务和虚构 API，禁止连接真实管理后台。
  const runId = "11111111-2222-4333-8444-555555555555";
  const document = {
    builtin_agents_version: 1,
    repositories: [{ id: "fixture", name: "诊断验收仓库", provider: "gitlab", project: "group/fixture", workspace: "./fixture",
      preflight: { enabled: true, failure_diagnosis_enabled: true, failure_diagnosis_agent: "ci-failure-diagnoser", steps: [{name: "test-all", command: ["bun", "test"]}] } }],
    providers: { gitlab: {kind: "gitlab", base_url: "https://gitlab.example.com/api/v4", token_env: "GITLAB_TOKEN"} },
    rules: [], scheduled_rules: [], skills: {},
    agents: { "ci-failure-diagnoser": { builtin_kind: "ci_failure_diagnosis", prompt: "分析日志并输出中文错误报告", sandbox: "read-only", timeout_seconds: 600, max_tool_rounds: 32 } },
  };
  const diagnosis = {
    preflight_run_id: runId, agent_name: "ci-failure-diagnoser", status: "completed", agent_run_id: null,
    report: "## CI 失败报告\n\n- 失败用例：同 deck 并发运行\n- 位置：`slideWorkflow.test.ts:414`\n\n```text\nExpected promise that rejects\nReceived promise that resolved\n```\n\n**原因推断：** 测试使用固定延迟，需进一步核对同步时机。",
    error: null, comment_error: null, created_at: 1792489610, finished_at: 1792489832,
  };
  const detail = {
    run_id: runId, repository_id: "fixture", number: 115, head_sha: "a".repeat(40), config_revision: "fixture",
    source_generation: 1, phase: "finished", status: "failure", attempts: 1, status_published: true, trigger_source: "event",
    branch: "feature/fixture", started_at: 1792489610, finished_at: 1792489832, failed_step: "test-all", exit_code: 1,
    error: null, output: "1 个测试失败，退出码 1", steps: [], linked_events: [], diagnosis,
  };
  await page.addInitScript(() => {
    if (location.protocol === "http:") localStorage.clear();
    window.__diagnosisFixture = { mutations: [] };
  });
  await page.route("**/api/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() !== "GET") {
      await page.evaluate(path => window.__diagnosisFixture.mutations.push(path), url.pathname);
      return route.abort("blockedbyclient");
    }
    if (url.pathname.endsWith("/stream")) return route.fulfill({ contentType: "text/event-stream", body: "event: end\ndata: {}\n\n" });
    let body = {};
    if (url.pathname === "/api/config") body = {revision: "fixture", document};
    else if (url.pathname.endsWith("/workspace/warmup")) body = null;
    else if (url.pathname === "/api/status") body = {paused: false, running_cycle: false, dispatching_events: false, config_revision: "fixture", stats: {runs: {}, events: {}, change_requests: {}}};
    else if (url.pathname === "/api/options") body = {events: []};
    else if (url.pathname === `/api/preflight-runs/${runId}`) body = detail;
    else if (["/api/config/versions", "/api/runs", "/api/preflight-runs", "/api/repositories/workspaces"].includes(url.pathname)) body = [];
    else if (["/api/events", "/api/change-requests"].includes(url.pathname)) body = {items: [], total: 0, page: 1, page_size: 10, total_pages: 1};
    else if (url.pathname === "/api/codex/runtime-options") body = {models: [], catalog_source: "unavailable", inherited_model: {value: null}, binary: {}, model_cache: {}, user_mcp_servers: [], managed_sandbox: {}, inherited_settings: {}};
    return route.fulfill({json: body});
  });
  await page.goto("about:blank");
  await page.goto(`http://127.0.0.1:38147/#preflight=${runId}`);
  return {runId, expectedReport: diagnosis.report};
}
