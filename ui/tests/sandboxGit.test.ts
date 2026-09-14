import assert from "node:assert/strict";
import test from "node:test";
import { presentRunLogs } from "../src/runLogPresentation.ts";

test("curl 不可用和 HTTP TLS 错误不能展示成 Git 阻断", () => {
  const messages = presentRunLogs([
    {id: 1, run_id: "run", created_at: 1, stream: "system", event_type: "run.curl_unavailable",
      payload: JSON.stringify({message: "请配置兼容 curl", error_code: "sandbox_curl_unavailable"})},
    {id: 2, run_id: "run", created_at: 2, stream: "system", event_type: "run.http_tls_unavailable",
      payload: JSON.stringify({message: "HTTP TLS 不可用", error_code: "sandbox_http_tls_unavailable"})},
  ]);
  for (const message of messages) {
    assert.equal(message.kind, "warning");
    assert.doesNotMatch(message.title, /Git|阻断/);
    assert.doesNotMatch(message.detail, /停止自动重试/);
  }
  assert.match(messages[0].title, /任务可继续/);
});

test("curl 程序已就绪但网络探测失败时保留具体阶段与告警语义", () => {
  const [message] = presentRunLogs([{
    id: 3, run_id: "run", created_at: 3, stream: "system", event_type: "run.curl_warning",
    payload: JSON.stringify({message: "请检查网络或 CA，无需重装 OpenSSL。", curl_binary: "D:/runtimes/bin/curl.exe",
      ssl_backend: "LibreSSL/4.3.2", https_probe: "failed", reason: "https_probe_failed"}),
  }]);
  assert.equal(message.kind, "warning");
  assert.match(message.title, /程序已就绪/);
  assert.match(message.body, /无需重装/);
  assert.match(message.detail, /D:\/runtimes\/bin\/curl.exe/);
  assert.match(message.detail, /LibreSSL\/4.3.2/);
  assert.match(message.detail, /HTTPS 探测：failed/);
});

test("沙盒 Git 故障展示真实原因、错误码与停止重试提示", () => {
  const messages = presentRunLogs([{
    id: 1, run_id: "run", created_at: 1, stream: "system",
    event_type: "run.git_https_failed",
    payload: JSON.stringify({error: "Git HTTPS 证书校验失败", error_code: "sandbox_git_certificate_invalid", retryable: false}),
  }]);
  assert.equal(messages[0].kind, "error");
  assert.match(messages[0].title, /已阻断运行/);
  assert.equal(messages[0].body, "Git HTTPS 证书校验失败");
  assert.match(messages[0].detail, /sandbox_git_certificate_invalid/);
  assert.match(messages[0].detail, /停止自动重试/);
});

test("探测成功展示主机和 SHA，跳过探测不显示错误", () => {
  const messages = presentRunLogs([
    {id: 1, run_id: "run", created_at: 1, stream: "system", event_type: "run.git_https_ready",
      payload: JSON.stringify({ssl_backend: "openssl", host: "github.com", sha: "a".repeat(40)})},
    {id: 2, run_id: "run", created_at: 2, stream: "system", event_type: "run.git_https_skipped",
      payload: JSON.stringify({reason: "Agent 禁止联网，未执行 HTTPS 探测"})},
  ]);
  assert.equal(messages[0].kind, "system");
  assert.match(messages[0].detail, /openssl/);
  assert.match(messages[0].detail, /github.com/);
  assert.match(messages[0].detail, /a{40}/);
  assert.equal(messages[1].kind, "system");
  assert.match(messages[1].body, /禁止联网/);
});

test("目录信任记录精确路径，所有权错误不误报 HTTPS 故障", () => {
  const messages = presentRunLogs([
    {id: 1, run_id: "run", created_at: 1, stream: "system", event_type: "run.git_workspace_trusted",
      payload: JSON.stringify({trusted_workspace: "D:/worktrees/run-id", reason: "只信任本轮已校验的 Git 工作区"})},
    {id: 2, run_id: "run", created_at: 2, stream: "system", event_type: "run.git_https_failed",
      payload: JSON.stringify({error_code: "sandbox_git_ownership_mismatch", retryable: false, error: "工作区所有权校验失败"})},
  ]);
  assert.equal(messages[0].kind, "system");
  assert.match(messages[0].detail, /D:\/worktrees\/run-id/);
  assert.equal(messages[1].kind, "error");
  assert.match(messages[1].title, /工作区所有权校验失败/);
  assert.doesNotMatch(messages[1].title, /HTTPS/);
  assert.match(messages[1].detail, /停止自动重试/);
});
