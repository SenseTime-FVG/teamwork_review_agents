import assert from "node:assert/strict";
import test from "node:test";
import { presentRunLogs } from "../src/runLogPresentation.ts";

test("额度跳过与新请求选模显示中文原因、范围及实际模型", () => {
  const logs = [
    { event_type: "model.quota_exhausted", model: "A", message: "本次运行跳过此候选；新运行重新检查。", reason: "insufficient_quota" },
    { event_type: "model.request_started", model: "B", message: "新请求按主链顺序选模。" },
  ].map((item, id) => ({
    id, run_id: "test", created_at: id, stream: "system", event_type: item.event_type,
    payload: JSON.stringify({ ...item, provider_id: "provider", request_round: id + 1 }),
  }));
  const messages = presentRunLogs(logs);
  assert.equal(messages[0].title, "额度耗尽，本次运行跳过该模型");
  assert.equal(messages[0].kind, "warning");
  assert.match(messages[0].body, /新运行重新检查/);
  assert.match(messages[0].detail, /insufficient_quota/);
  assert.equal(messages[1].title, "新一轮请求选模");
  assert.equal(messages[1].kind, "system");
  assert.match(messages[1].detail, /第 2 轮 · provider \/ B/);
});
