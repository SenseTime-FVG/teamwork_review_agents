export type OverviewRefreshState = {
  busy: boolean;
  error: string;
};

export function createOverviewRefresher<T>(
  load: (path: string) => Promise<T>,
  accept: (result: T) => void,
  publish: (state: OverviewRefreshState) => void,
) {
  let scope: string | undefined;
  let generation = 0;
  let pending: Promise<void> | null = null;
  let state: OverviewRefreshState = { busy: false, error: "" };

  const update = (next: OverviewRefreshState) => {
    state = next;
    publish(next);
  };
  const invalidate = () => {
    // 筛选或分页改变时立即隔离旧响应，包括迟到的错误及按钮收尾。
    generation++;
    scope = undefined;
    pending = null;
    update({ busy: false, error: "" });
  };

  return {
    invalidate,
    refresh(path: string, manual = false): Promise<void> {
      if (scope !== path) invalidate();
      scope = path;
      // 同范围的轮询、手动点击共用请求，慢响应不会被三秒轮询不断作废。
      if (manual) update({ busy: true, error: "" });
      if (pending) return pending;
      const request = ++generation;
      pending = Promise.resolve().then(() => load(path)).then((result) => {
        if (generation !== request) return;
        accept(result);
        update({ ...state, error: "" });
      }).catch((reason: unknown) => {
        if (generation !== request) return;
        // 失败不替换现有记录；每个区域自行报错，不阻断其他列表。
        update({ ...state, error: reason instanceof Error ? reason.message : "请求失败，请稍后重试" });
      }).finally(() => {
        if (generation !== request) return;
        pending = null;
        update({ ...state, busy: false });
      });
      return pending;
    },
  };
}
