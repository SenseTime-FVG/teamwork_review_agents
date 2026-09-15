import type { Repository } from "./types";

// 顶部约束整体范围；仅全部仓库模式允许列表独立筛选，保留原有排序等条件。
export type OverviewSortField = "updated_at" | "number" | "scanned_at" | "latest_event_at" | "occurred_at";

export type OverviewFilter = {
  repositoryId: string;
  number: string;
  status: string;
  statuses: string[];
  limit: number | null;
  page: number;
  sortBy: OverviewSortField;
  sortDirection: "asc" | "desc";
};

export function toggleOverviewSort(filter: OverviewFilter, sortBy: OverviewSortField): OverviewFilter {
  // 切换列时默认降序，重复点击当前列时反转方向，其他筛选条件保持不变。
  return {
    ...filter,
    page: 1,
    sortBy,
    sortDirection: filter.sortBy === sortBy && filter.sortDirection === "desc" ? "asc" : "desc",
  };
}

export function overviewQuery(filter: OverviewFilter, repositoryId: string, includeNumber = false): string {
  const parameters = new URLSearchParams();
  parameters.set("page", String(filter.page));
  parameters.set("sort_by", filter.sortBy);
  parameters.set("sort_direction", filter.sortDirection);
  if (filter.limit === null) {
    parameters.set("all_records", "true");
  } else {
    parameters.set("limit", String(filter.limit));
  }
  const effectiveRepositoryId = repositoryId || filter.repositoryId;
  if (effectiveRepositoryId) parameters.set("repository_id", effectiveRepositoryId);
  if (includeNumber && /^\d+$/.test(filter.number) && Number(filter.number) > 0) {
    parameters.set("number", filter.number);
  }
  if (filter.statuses.length > 0) {
    filter.statuses.forEach((status) => parameters.append("status", status));
  } else if (filter.status) {
    parameters.set("status", filter.status);
  }
  return parameters.toString();
}

export function overviewStatusPath(repositoryId: string): string {
  // 全部仓库沿用原接口；仓库名称不参与查询，特殊字符由 URL 编码处理。
  return repositoryId
    ? `/api/status?${new URLSearchParams({ repository_id: repositoryId })}`
    : "/api/status";
}

export function overviewRepositoryOptions(repositories: Pick<Repository, "id" | "display_name">[]) {
  // 以最终展示名称判断冲突，停用仓库和无展示名称的旧配置也参与同名检测。
  const names = repositories.map((repository) => repository.display_name?.trim() || repository.id);
  const counts = new Map<string, number>();
  for (const name of names) counts.set(name, (counts.get(name) ?? 0) + 1);
  return [
    { value: "", label: "全部仓库" },
    ...repositories.map((repository, index) => ({
      value: repository.id,
      label: (counts.get(names[index]) ?? 0) > 1 ? `${names[index]}（${repository.id}）` : names[index],
    })),
  ];
}
