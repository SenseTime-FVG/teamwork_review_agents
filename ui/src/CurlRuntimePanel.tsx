import { useEffect, useState } from "react";
import { api } from "./api";

type CurlRuntimeStatus = {
  status: string;
  message: string;
  source?: string;
  curl_binary?: string;
  ssl_backend?: string;
  error_code?: string;
  cache_directory?: string;
  offline_archive?: string | null;
  offline_download_url?: string;
  candidates?: Array<{ path: string; code: string; message?: string }>;
};

export function CurlRuntimePanel() {
  const [status, setStatus] = useState<CurlRuntimeStatus | null>(null);
  const [error, setError] = useState("");
  const [requesting, setRequesting] = useState(false);

  useEffect(() => {
    // 状态查询不触发安装；组件卸载后不再更新状态或继续轮询。
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const next = await api<CurlRuntimeStatus>("/api/runtime/curl");
        if (active) { setStatus(next); setError(""); }
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : "无法读取运行环境状态");
      } finally {
        if (active) timer = setTimeout(refresh, 2500);
      }
    }
    void refresh();
    return () => { active = false; clearTimeout(timer); };
  }, []);

  async function prepare() {
    setRequesting(true);
    try {
      const next = await api<CurlRuntimeStatus>("/api/runtime/curl/prepare", { method: "POST" });
      setStatus(next);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法开始准备");
    } finally {
      setRequesting(false);
    }
  }

  if (status?.status === "not_applicable") return null;
  const warning = Boolean(error || status?.status === "unavailable");
  const preparing = requesting || status?.status === "preparing";
  return (
    <section className="section-card curl-runtime-panel">
      <div className="section-title-row">
        <div><h2>Windows HTTPS 运行环境</h2><p>由服务自动准备，无需手动安装 OpenSSL；使用已保存的配置。</p></div>
        <button type="button" className="button secondary" disabled={preparing} aria-busy={preparing} onClick={() => void prepare()}>
          {preparing ? "正在准备…" : "重新检查并准备"}
        </button>
      </div>
      <div className={`curl-runtime-status ${warning ? "is-warning" : ""}`} role="status">
        {error || status?.message || "正在读取状态…"}
      </div>
      {status?.curl_binary && <p>当前程序：<code>{status.curl_binary}</code>{status.ssl_backend && ` · ${status.ssl_backend}`}</p>}
      <details>
        <summary>诊断与离线部署</summary>
        {status?.error_code && <p>诊断代码：<code>{status.error_code}</code></p>}
        {status?.candidates?.map((candidate) => <p key={candidate.path}><code>{candidate.path}</code><br />{candidate.message || candidate.code}</p>)}
        {status?.cache_directory && <p>项目缓存：<code>{status.cache_directory}</code></p>}
        {status?.offline_download_url && <p><a href={status.offline_download_url} target="_blank" rel="noreferrer">下载对应架构的官方离线包</a></p>}
        {status?.offline_archive && <p>离线部署：将对应官方 ZIP 原样放到 <code>{status.offline_archive}</code>，再点击“重新检查并准备”。服务会校验固定版本与摘要，无需解压或配置环境变量。</p>}
        <p>程序就绪不代表所有仓库的 HTTPS 已通过；实际请求仍受 Agent 网络权限、代理和可信证书约束。</p>
      </details>
    </section>
  );
}
