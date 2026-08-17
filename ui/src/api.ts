import type { RunLog } from "./types";

const TOKEN_KEY = "teamwork-review-agents-admin-token";

export function getToken(): string {
  return window.localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string): void {
  if (token) {
    window.localStorage.setItem(TOKEN_KEY, token);
  } else {
    window.localStorage.removeItem(TOKEN_KEY);
  }
}

function headers(extra?: HeadersInit): Headers {
  const result = new Headers(extra);
  result.set("Content-Type", "application/json");
  const token = getToken();
  if (token) {
    result.set("Authorization", `Bearer ${token}`);
  }
  return result;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: headers(init?.headers) });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail ?? `请求失败：${response.status}`);
  }
  return body as T;
}

export async function streamRunLogs(
  runId: string,
  afterId: number,
  signal: AbortSignal,
  onLog: (log: RunLog) => void,
): Promise<void> {
  const response = await fetch(
    `/api/runs/${encodeURIComponent(runId)}/stream?after_id=${afterId}`,
    { headers: headers(), signal },
  );
  if (!response.ok || !response.body) {
    throw new Error(`日志流连接失败：${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const event = frame.match(/^event:\s*(.+)$/m)?.[1];
      const data = frame.match(/^data:\s*(.+)$/m)?.[1];
      if (event !== "end" && data) {
        onLog(JSON.parse(data) as RunLog);
      }
    }
  }
}
