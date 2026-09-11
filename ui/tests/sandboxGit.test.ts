import assert from "node:assert/strict";
import test from "node:test";
import { presentRunLogs } from "../src/runLogPresentation.ts";

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
