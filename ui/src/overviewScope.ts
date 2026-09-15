// 仓库范围只有顶部一个来源，列表分别保留分页、状态、编号与排序条件。
export type OverviewSortField = "updated_at" | "number" | "scanned_at" | "latest_event_at" | "occurred_at";

export type OverviewFilter = {
  number: string;
  status: string;
  statuses: string[];
  limit: number | null;
  page: number;
  sortBy: OverviewSortField;
  sortDirection: "asc" | "desc";
};

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
  if (repositoryId) parameters.set("repository_id", repositoryId);
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
