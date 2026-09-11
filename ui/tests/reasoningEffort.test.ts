import assert from "node:assert/strict";
import test from "node:test";
import { EXTERNAL_REASONING_LEVELS, reasoningEffortOptions } from "../src/reasoningEffort.ts";
import { presentRunLogs } from "../src/runLogPresentation.ts";

test("外部选项包含 xhigh/max，过滤所有来源的 minimal", () => {
  assert.deepEqual(reasoningEffortOptions(EXTERNAL_REASONING_LEVELS), ["low", "medium", "high", "xhigh", "max"]);
  assert.deepEqual(reasoningEffortOptions(["minimal", "high"], "minimal"), ["high"]);
  assert.deepEqual(reasoningEffortOptions(["high"], "custom"), ["high", "custom"]);
});

test("时间线显示降级原因和不传参数语义，不误报任务终止", () => {
  const logs = [
    { from: "max", to: "xhigh" },
    { from: "low", to: null },
  ].map((step, index) => ({
    id: index, run_id: "test", created_at: 1, stream: "system",
    event_type: "model.reasoning_downgraded",
    payload: JSON.stringify({ ...step, provider_id: "provider", model: "gpt-test", reason: "Unsupported value" }),
  }));
  const messages = presentRunLogs(logs);
  assert.equal(messages[0].title, "推理强度自动降级");
  assert.equal(messages[0].kind, "system");
  assert.match(messages[0].body, /max.*xhigh/);
  assert.match(messages[1].body, /去掉 effort 参数/);
  assert.match(messages[1].detail, /Unsupported value/);
});
