export type OverviewSelectionRow = { key: string; selectable: boolean };
export type OverviewSelectionAnchor = { key: string; visibleKeys: string[] };

export function overviewSelectionRange(
  rows: readonly OverviewSelectionRow[],
  key: string,
  anchor: OverviewSelectionAnchor | null,
  shiftKey: boolean,
): { keys: string[]; anchor: OverviewSelectionAnchor } | null {
  // 起点按记录 ID 绑定，顺序变化后不沿用旧位置，禁用目标不改变任何选择。
  const target = rows.findIndex((row) => row.key === key);
  if (target < 0 || !rows[target].selectable) return null;
  const visibleKeys = rows.map((row) => row.key);
  const sameOrder = anchor !== null
    && anchor.visibleKeys.length === visibleKeys.length
    && anchor.visibleKeys.every((value, index) => value === visibleKeys[index]);
  if (!shiftKey || !sameOrder || anchor === null) return { keys: [key], anchor: { key, visibleKeys } };
  const start = visibleKeys.indexOf(anchor.key);
  if (start < 0) return { keys: [key], anchor: { key, visibleKeys } };
  return {
    keys: rows.slice(Math.min(start, target), Math.max(start, target) + 1)
      .filter((row) => row.selectable).map((row) => row.key),
    anchor,
  };
}

export function toggleOverviewSelection(current: readonly string[], target: string, keys: readonly string[]): string[] {
  // 区间统一采用目标的反选状态，其他已经勾选的项不受影响。
  const affected = new Set(keys);
  if (current.includes(target)) return current.filter((key) => !affected.has(key));
  return [...new Set([...current, ...keys])];
}
