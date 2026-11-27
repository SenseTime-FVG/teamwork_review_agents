export function createSingleFlight<T>(load: () => Promise<T>): () => Promise<T> {
  // 轮询和主动刷新复用同一个进行中的请求；成功或失败后均允许重新读取。
  let pending: Promise<T> | null = null;
  return () => {
    if (pending) return pending;
    pending = Promise.resolve().then(load).finally(() => {
      pending = null;
    });
    return pending;
  };
}
