// 仓库虚拟环境配置、环境校验与缺依赖日志呈现回归。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { presentRunLogs } from "../src/runLogPresentation.ts";

test("仓库配置提供可选虚拟环境和模块校验，清空路径会清空模块", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.ok(app.includes('label="Python 虚拟环境路径（可选）"'));
  assert.ok(app.includes('label="启动前校验 Python 模块（可选）"'));
  assert.ok(app.includes("python_venv: value || null"));
  assert.ok(app.includes("!value.trim() ? { python_check_modules: [] }"));
  assert.ok(app.includes("disabled={!agentWorkspace.python_venv?.trim()}"));
});

test("环境成功显示解释器和版本，失败明确测试未执行", () => {
  const records = [
    { event_type: "workspace.python.ready", executable: "/checkout/.venv/bin/python", prefix: "/checkout/.venv", version: "3.12.8", source: "restored", checked_modules: ["pytest"] },
    { event_type: "workspace.python.failed", source: "created", status: "error", error: "环境未就绪，相关测试未执行", exit_code: 1, output: "ModuleNotFoundError" },
  ].map((item, id) => ({ id, run_id: "test", created_at: id, stream: "system", event_type: item.event_type, payload: JSON.stringify(item) }));
  const [ready, failed] = presentRunLogs(records);
  assert.equal(ready.title, "工作区 Python 环境已接入");
  assert.match(ready.detail, /\/checkout\/\.venv\/bin\/python/);
  assert.match(ready.detail, /3\.12\.8/);
  assert.match(ready.detail, /pytest/);
  assert.equal(failed.kind, "error");
  assert.match(failed.title, /测试未执行/);
  assert.match(failed.detail, /退出码：1/);
});

test("收集缺依赖不误称代码测试失败，普通断言失败保持原展示", () => {
  const collectionError = "ERROR collecting tests/test_memory.py\nModuleNotFoundError: No module named 'anthropic'\nInterrupted: 9 errors during collection";
  const records = [
    { output: collectionError, exitCode: 2 },
    { output: "AssertionError: expected result\n1 failed, 5 passed", exitCode: 2 },
    { output: collectionError, exitCode: null },
  ].map(({ output, exitCode }, id) => ({ id, run_id: "test", created_at: id, stream: "stdout", event_type: "item.completed", payload: JSON.stringify({ item: { type: "command_execution", command: "python -m pytest", exit_code: exitCode, aggregated_output: output } }) }));
  const [missing, failed, running] = presentRunLogs(records);
  assert.equal(missing.title, "测试环境未就绪 · 收集阶段缺少依赖");
  assert.match(missing.detail, /未实际执行/);
  assert.match(missing.detail, /anthropic/);
  assert.equal(failed.title, "命令已结束 · 退出码 2");
  assert.doesNotMatch(failed.detail, /环境未就绪/);
  // 未结束的命令不能仅凭实时输出提前标记为环境准备失败。
  assert.equal(running.title, "运行命令");
  assert.equal(running.kind, "command");
});
