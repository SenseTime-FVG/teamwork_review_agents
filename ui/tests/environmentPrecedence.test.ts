import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// 保证三个配置入口对同名变量优先级的说明一致，不访问真实配置或后台。
const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");

test("全局环境显示仓库、Agent、全局的高到低优先级", () => {
  assert.match(app, /title="全局环境变量"\s+description="优先级从高到低：仓库 > Agent > 全局/);
  assert.match(app, /props.description \?\? "优先级从高到低：仓库 > Agent > 全局；同名变量按整项配置覆盖/);
});

test("仓库明确覆盖 Agent 和全局，Agent 明确被仓库覆盖", () => {
  assert.match(app, /title="仓库环境变量"[\s\S]*?仓库环境变量会覆盖 Agent 和全局的同名变量，包括值、来源和暴露开关/);
  assert.match(app, /title="Agent 环境变量"\s+description="Agent 环境变量覆盖全局默认，但会被仓库同名变量整项覆盖/);
  assert.doesNotMatch(app, /普通变量会覆盖全局和仓库配置|优先级由全局到仓库再到 Agent/);
});
