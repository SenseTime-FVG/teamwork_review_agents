export function preflightRunIdFromHash(hash: string): string | null {
  // 链接只允许运行 ID；不读取 Token，也不允许片段注入 API 路径或外部地址。
  const values = new URLSearchParams(hash.replace(/^#/, "")).getAll("preflight");
  return values.length === 1 && /^[A-Za-z0-9_-]{1,128}$/.test(values[0]) ? values[0] : null;
}

export function withoutPreflightHash(href: string): string {
  // 关闭详情只移除本功能的参数，保留原有路径、查询与其他片段参数。
  const url = new URL(href);
  const parameters = new URLSearchParams(url.hash.slice(1));
  parameters.delete("preflight");
  url.hash = parameters.toString();
  return url.href;
}
