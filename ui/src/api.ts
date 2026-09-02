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

function headers(extra?: HeadersInit, contentType = true): Headers {
  const result = new Headers(extra);
  if (contentType) {
    result.set("Content-Type", "application/json");
  }
  const token = getToken();
  if (token) {
    result.set("Authorization", `Bearer ${token}`);
  }
  return result;
}

export type ManagedPromptFile = {
  name: string;
  path: string;
  size: number;
};

export type ManagedSkillDirectory = {
  directory?: string;
  path: string;
  resolved_path?: string;
  name: string;
  description: string;
  valid: boolean;
  managed?: boolean;
  editable?: boolean;
  body?: string;
  error?: string | null;
};

export type ManagedSkillDocument = ManagedSkillDirectory & {
  directory: string;
  managed: true;
  editable: true;
  body: string;
};

export type ManagedSkillDocumentInput = {
  name: string;
  description: string;
  body: string;
};

export async function uploadPromptFile(file: File): Promise<ManagedPromptFile> {
  const body = new FormData();
  body.set("file", file);
  const response = await fetch("/api/prompt-files/import", {
    method: "POST",
    headers: headers(undefined, false),
    body,
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(result.detail ?? `导入失败：${response.status}`);
  }
  return result as ManagedPromptFile;
}

export async function uploadSkillDirectory(
  files: File[],
): Promise<ManagedSkillDirectory> {
  const body = new FormData();
  for (const file of files) {
    const relativePath = file.webkitRelativePath || file.name;
    body.append("files", file, relativePath);
  }
  const response = await fetch("/api/skill-directories/import", {
    method: "POST",
    headers: headers(undefined, false),
    body,
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(result.detail ?? `导入失败：${response.status}`);
  }
  return result as ManagedSkillDirectory;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: headers(init?.headers) });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail ?? `请求失败：${response.status}`);
  }
  return body as T;
}

export async function createManagedSkill(
  input: ManagedSkillDocumentInput,
): Promise<ManagedSkillDocument> {
  return api<ManagedSkillDocument>("/api/skill-directories", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function loadManagedSkill(
  directory: string,
): Promise<ManagedSkillDocument> {
  return api<ManagedSkillDocument>(
    `/api/skill-directories/${encodeURIComponent(directory)}/document`,
  );
}

export async function updateManagedSkill(
  directory: string,
  input: ManagedSkillDocumentInput,
): Promise<ManagedSkillDocument> {
  return api<ManagedSkillDocument>(
    `/api/skill-directories/${encodeURIComponent(directory)}/document`,
    {
      method: "PUT",
      body: JSON.stringify(input),
    },
  );
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

export async function streamPreflightLogs(
  runId: string,
  afterId: number,
  signal: AbortSignal,
  onLog: (log: RunLog) => void,
): Promise<void> {
  const response = await fetch(
    `/api/preflight-runs/${encodeURIComponent(runId)}/stream?after_id=${afterId}`,
    { headers: headers(), signal },
  );
  if (!response.ok || !response.body) {
    throw new Error(`CI 日志流连接失败：${response.status}`);
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
