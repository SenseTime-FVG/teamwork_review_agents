import type { OverviewFilter } from "./overviewScope";

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

export function toggleOverviewRecords<T>(
  current: T[],
  visible: readonly T[],
  keyOf: (item: T) => string,
  target: string,
  keys: readonly string[],
): T[] {
  // 仅保存已选记录，跨页切换时从原集合补齐不在当前页的目标。
  const records = new Map([...current, ...visible].map((item) => [keyOf(item), item]));
  const selectedKeys = toggleOverviewSelection(current.map(keyOf), target, keys);
  return selectedKeys.flatMap((key) => {
    const item = records.get(key);
    return item === undefined ? [] : [item];
  });
}

export function refreshOverviewSelection<T>(current: T[], visible: readonly T[], keyOf: (item: T) => string): T[] {
  // 当前页响应只能更新已选快照，不能把未出现的其他页记录认定为删除。
  const records = new Map(visible.map((item) => [keyOf(item), item]));
  let changed = false;
  const next = current.map((item) => {
    const latest = records.get(keyOf(item));
    if (latest === undefined || latest === item) return item;
    changed = true;
    return latest;
  });
  return changed ? next : current;
}

export function overviewSelectionScopeChanged(current: OverviewFilter, next: OverviewFilter): boolean {
  // 排序、页码和每页条数不改变选择范围；多状态顺序不影响实际筛选。
  const statuses = (filter: OverviewFilter) => [...new Set(
    filter.statuses.length > 0 ? filter.statuses : filter.status ? [filter.status] : [],
  )].sort().join("\u0000");
  return current.repositoryId !== next.repositoryId
    || current.number !== next.number
    || statuses(current) !== statuses(next);
}
