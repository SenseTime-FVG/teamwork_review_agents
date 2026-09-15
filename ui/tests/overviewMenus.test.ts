import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fitOverlayToViewport } from "../src/overlayPlacement.ts";

test("菜单按内容拓宽，靠右或极长时留在视口内", () => {
  assert.deepEqual(fitOverlayToViewport(300, 500, 1440), { left: 300, width: 500 });
  assert.deepEqual(fitOverlayToViewport(1200, 500, 1440), { left: 932, width: 500 });
  assert.deepEqual(fitOverlayToViewport(100, 5000, 390), { left: 8, width: 374 });
  for (const viewport of [0, 10, 320, 390, 1440]) {
    const result = fitOverlayToViewport(-10, 5000, viewport);
    assert.ok(result.left >= 0);
    assert.ok(result.left + result.width <= viewport);
  }
});

test("仓库菜单不换行，保留视口上限与完整名称说明", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(app, /if \(props.fitContent\)/);
  assert.match(app, /fitOverlayToViewport\(bounds.left, menu.offsetWidth, viewportWidth\)/);
  assert.match(app, /title=\{props.fitContent \? option.label : undefined\}/);
  assert.match(css, /\.select-combobox-options.fit-content \{[^}]*width: max-content;[^}]*max-width: calc\(100vw - 16px\)/);
  assert.match(css, /\.select-combobox-option > span:last-child \{[^}]*text-overflow: ellipsis;[^}]*white-space: nowrap/);
});

test("全局按钮移除括号并接入两秒延迟提示，不使用原生即时标题", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const tooltip = readFileSync(new URL("../src/DelayedTooltipButton.tsx", import.meta.url), "utf8");
  const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.doesNotMatch(app, /立即扫描（全局）|恢复（全局）|暂停（全局）/);
  assert.equal((app.match(/description="该操作作用于全部仓库，不受顶部仓库筛选影响。"/g) ?? []).length, 2);
  assert.match(tooltip, /TOOLTIP_DELAY_MS = 2000/);
  assert.match(tooltip, /onMouseLeave=\{hide\}/);
  assert.match(tooltip, /onFocus=\{schedule\}/);
  assert.match(tooltip, /onBlur=\{hide\}/);
  assert.match(tooltip, /event.key === "Escape"/);
  assert.match(tooltip, /useEffect\(\(\) => clearTimer, \[\]\)/);
  assert.match(tooltip, /aria-describedby=\{visible \? tooltipId : undefined\}/);
  assert.match(tooltip, /role="tooltip"/);
  assert.doesNotMatch(tooltip, /\btitle=/);
  assert.match(css, /\.delayed-button-tooltip \{[^}]*background: #0b121c/);
});
