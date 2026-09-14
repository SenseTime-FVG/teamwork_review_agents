import assert from "node:assert/strict";
import test from "node:test";
import { presentRunLogs } from "../src/runLogPresentation.ts";

test("压缩记录保留不可变边界，并区分预算估算和最终结果", () => {
  const [message] = presentRunLogs([{
    id: 1, run_id: "run", created_at: 1, stream: "system", event_type: "context.compacted",
    payload: JSON.stringify({provider_id: "a", model: "gpt-a", request_round: 3,
      before_estimated_tokens: 8000, after_estimated_tokens: 3000,
      input_budget: 9000, window_source: "configured", estimator: "utf8_bytes_conservative",
      retained_rounds: 2, summary_requests: 1, summary: "已完成检查，尚未推送", fixed_content_preserved: true}),
  }]);
  assert.equal(message.kind, "system");
  assert.equal(message.title, "上下文已压缩");
  assert.match(message.body, /系统指令、Skill、工具定义和原始任务保持不变/);
  assert.match(message.body, /8000 降至 3000/);
  assert.match(message.detail, /不是精确 Token/);
  assert.match(message.detail, /非最终结果/);
  assert.match(message.detail, /尚未推送/);
});

test("压缩失败停止整轮重试，恢复尝试和工具截短仅显示告警", () => {
  const messages = presentRunLogs([
    {id: 1, run_id: "run", created_at: 1, stream: "system", event_type: "context.compaction_failed",
      payload: JSON.stringify({error: "原始任务不能压缩", error_code: "context_fixed_content_too_large", retryable: false})},
    {id: 2, run_id: "run", created_at: 2, stream: "system", event_type: "context.retry_after_compaction",
      payload: JSON.stringify({message: "压缩后仅重试当前请求一次"})},
    {id: 3, run_id: "run", created_at: 3, stream: "system", event_type: "context.tool_output_truncated",
      payload: JSON.stringify({message: "完整结果仍保留", call_id: "call-1", original_bytes: 20000})},
    {id: 4, run_id: "run", created_at: 4, stream: "system", event_type: "model.attempt_failed",
      payload: JSON.stringify({provider_id: "a", model: "gpt-a", phase: "compaction", reason: "额度耗尽"})},
  ]);
  assert.equal(messages[0].kind, "error");
  assert.match(messages[0].detail, /停止整轮自动重试/);
  assert.match(messages[0].detail, /context_fixed_content_too_large/);
  assert.equal(messages[1].kind, "warning");
  assert.equal(messages[2].kind, "warning");
  assert.match(messages[2].detail, /call-1.*20000/);
  assert.match(messages[3].detail, /历史压缩请求/);
});
