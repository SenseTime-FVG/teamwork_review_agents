import type { CodexRuntimeOptions } from "./types.ts";

export type CodexCatalogState = {
  options: CodexRuntimeOptions;
  busy: boolean;
  error: string;
};

export function createCodexCatalogRefresher(
  initial: CodexRuntimeOptions,
  load: () => Promise<CodexRuntimeOptions>,
  publish: (state: CodexCatalogState) => void,
) {
  let scope: string | undefined;
  let generation = 0;
  let options = initial;
  let pending: Promise<void> | null = null;

  return {
    refresh(key: string): Promise<void> {
      // 同一配置合并自动/手动请求；切换配置时作废旧响应和旧账号目录。
      if (scope === key && pending) return pending;
      if (scope !== key) options = initial;
      scope = key;
      const request = ++generation;
      publish({ options, busy: true, error: "" });
      pending = Promise.resolve().then(load).then((result) => {
        if (generation !== request) return;
        options = result;
        publish({ options, busy: false, error: "" });
      }).catch(() => {
        if (generation !== request) return;
        // 不回显原始响应；保留同一配置的上次结果，并明确它不是新查询结果。
        publish({ options, busy: false, error: "模型目录刷新失败，当前保留上次结果；请检查后台连接后重试。" });
      }).finally(() => {
        if (generation === request) pending = null;
      });
      return pending;
    },
  };
}

export function codexCatalogSourceLabel(options: CodexRuntimeOptions): string {
  if (options.catalog_source === "app_server") return "当前 CLI · model/list";
  if (options.catalog_source === "account_cache") return "账号缓存（回退，可能过期）";
  if (options.catalog_source === "bundled") return "CLI 内置参考目录（回退）";
  return "目录不可用";
}
