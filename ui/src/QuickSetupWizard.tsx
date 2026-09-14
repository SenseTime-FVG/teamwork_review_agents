import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { ConfigDocument } from "./types";
import { initialSetupRules, setupAgents, setupRuleDescription, setupRules } from "./quickSetup";
import { SetupIntentInput } from "./SetupIntentInput";
import { SetupExitConfirmation } from "./SetupExitConfirmation";
import "./quickSetup.css";

type Summary = {
  repository_id: string; workspace: string; provider: string; project: string; token_env: string;
  rules: Array<{ name: string; kind: string; enabled: boolean; repositories: string[]; applies: boolean }>;
  agent_skills: Record<string, string[]>;
};
type Check = { name: string; ok: boolean; detail: string };
type Props = {
  document: ConfigDocument; revision: string;
  onClose: () => void;
  onSaved: (document: ConfigDocument, revision: string) => void;
};
const steps = ["选择平台", "仓库与凭证", "选择规则", "配置 Skill", "检查与完成"];

// 向导仅保留内存草稿，取消或关闭后不保存 Token，不覆盖共享 Agent。
export function QuickSetupWizard(props: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [step, setStep] = useState(0);
  const [platform, setPlatform] = useState("github");
  const [kind, setKind] = useState("github");
  const [baseUrl, setBaseUrl] = useState("https://api.github.com");
  const [remote, setRemote] = useState("");
  const [existing, setExisting] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [tokenSource, setTokenSource] = useState("value");
  const [token, setToken] = useState("");
  const [systemVariable, setSystemVariable] = useState("GITHUB_TOKEN");
  const [showToken, setShowToken] = useState(false);
  const [connectionVersion, setConnectionVersion] = useState(0);
  const [confirmExit, setConfirmExit] = useState(false);
  const rules = setupRules(props.document);
  const [selected, setSelected] = useState(() => initialSetupRules(rules));
  const [useSkills, setUseSkills] = useState(false);
  const [assignments, setAssignments] = useState<Record<string, string[]>>({});
  const [newSkills, setNewSkills] = useState<Record<string, string>>({});
  const [skillName, setSkillName] = useState("");
  const [skillPath, setSkillPath] = useState("");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [checks, setChecks] = useState<Check[] | null>(null);
  const [acceptFailedCheck, setAcceptFailedCheck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const agents = setupAgents(props.document, selected);
  const skillIds = [...new Set([...Object.keys(props.document.skills), ...Object.keys(newSkills)])];
  const tokenName = kind === "github" ? "GITHUB_TOKEN" : "GITLAB_TOKEN";

  useEffect(() => {
    dialog.current?.showModal();
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = previous; };
  }, []);

  // 离开检查页后清除旧结果，防止把其他地址或 Token 的成功结果用于新草稿。
  function changeStep(next: number) {
    setStep(next); setError(""); setSummary(null); setChecks(null); setAcceptFailedCheck(false);
    dialog.current?.querySelector(".quick-setup-body")?.scrollTo({ top: 0 });
  }

  function choosePlatform(value: string) {
    setConnectionVersion((version) => version + 1); setShowToken(false);
    setPlatform(value); setExisting(""); setToken(""); setTokenSource("value");
    setRemote(""); setDisplayName(""); setSelected(initialSetupRules(rules)); setUseSkills(false); setAssignments({});
    setKind(value === "gitlab" ? "gitlab" : "github");
    setBaseUrl(value === "custom" ? "" : value === "gitlab" ? "https://gitlab.com/api/v4" : "https://api.github.com");
    setSystemVariable(value === "gitlab" ? "GITLAB_TOKEN" : "GITHUB_TOKEN");
  }

  function chooseRepository(id: string) {
    setConnectionVersion((version) => version + 1); setShowToken(false);
    setExisting(id); setSelected(initialSetupRules(rules, id || undefined)); setToken("");
    const repository = props.document.repositories.find((item) => item.id === id);
    if (!repository) {
      setRemote(""); setDisplayName(""); setTokenSource("value"); setUseSkills(false); setAssignments({});
      return;
    }
    const provider = props.document.providers[repository.provider];
    const providerKind = String(provider.kind);
    setKind(providerKind); setBaseUrl(String(provider.base_url));
    setPlatform(provider.base_url === "https://api.github.com" ? "github" : provider.base_url === "https://gitlab.com/api/v4" ? "gitlab" : "custom");
    const host = new URL(String(provider.base_url)).hostname.replace(/^api\.github\.com$/, "github.com");
    setRemote(repository.clone_url || (/^[\w.-]+@|:\/\//.test(repository.project) ? repository.project : `https://${host}/${repository.project}.git`));
    setDisplayName(repository.display_name || repository.id); setTokenSource("existing");
    setSystemVariable(String(provider.token_env));
    setUseSkills(repository.allowed_skills !== null && Object.entries(props.document.agents).some(([name, agent]) =>
      (repository.agent_skills?.[name] ?? agent.skills ?? []).some((skill) => !repository.allowed_skills?.length || repository.allowed_skills.includes(skill))));
    // 已有仓库未覆盖的 Agent 使用原列表，再由仓库白名单收窄。
    setAssignments(Object.fromEntries(Object.entries(props.document.agents).map(([name, agent]) => [name,
      (repository.agent_skills?.[name] ?? agent.skills ?? []).filter((skill) => !repository.allowed_skills?.length || repository.allowed_skills.includes(skill)),
    ])));
  }

  function requestBody() {
    return {
      revision: props.revision, kind, base_url: baseUrl, remote, display_name: displayName,
      existing_repository_id: existing || null, token_source: tokenSource,
      token: tokenSource === "value" ? token : "", token_system_variable: systemVariable,
      event_rules: rules.filter(({ key, kind }) => selected.includes(key) && kind === "event").map(({ rule }) => rule.name),
      scheduled_rules: rules.filter(({ key, kind }) => selected.includes(key) && kind === "scheduled").map(({ rule }) => rule.name),
      use_skills: useSkills, new_skills: useSkills ? newSkills : {},
      agent_skills: useSkills ? Object.fromEntries(agents.map((name) => [name, assignments[name] ?? []])) : {},
    };
  }

  async function preview() {
    setBusy(true); setError(""); setChecks(null); setAcceptFailedCheck(false);
    try {
      const result = await api<Summary>("/api/setup/preview", { method: "POST", body: JSON.stringify(requestBody()) });
      setSummary(result); setStep(4);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "配置检查失败"); }
    finally { setBusy(false); }
  }

  async function checkConnection() {
    setBusy(true); setError(""); setAcceptFailedCheck(false);
    try {
      const result = await api<{ checks: Check[] }>("/api/setup/check", { method: "POST", body: JSON.stringify(requestBody()) });
      setChecks(result.checks);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "连接检查失败"); }
    finally { setBusy(false); }
  }

  async function complete() {
    setBusy(true); setError("");
    try {
      const result = await api<{ document: ConfigDocument; revision: string }>("/api/setup/complete", { method: "POST", body: JSON.stringify(requestBody()) });
      setToken(""); props.onSaved(result.document, result.revision);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "保存失败"); }
    finally { setBusy(false); }
  }

  function close() {
    if (!busy) setConfirmExit(true);
  }
  const connectionReady = Boolean(baseUrl.trim() && remote.trim() && (
    tokenSource === "existing" ? existing : tokenSource === "value" ? token.trim() : systemVariable.trim()
  ));
  const hasFailedCheck = checks?.some((check) => !check.ok);

  return <><dialog className="quick-setup" ref={dialog} aria-labelledby="quick-setup-title" onCancel={(event) => { event.preventDefault(); close(); }}>
    <header className="quick-setup-header"><div><span className="eyebrow">QUICK SETUP</span><h2 id="quick-setup-title">一键配置</h2><p>连接仓库，选择需要的规则，其余沿用现有配置。</p></div><button type="button" className="button secondary" disabled={busy} onClick={close}>取消</button></header>
    <ol className="quick-setup-steps" aria-label="配置步骤">{steps.map((title, index) => <li key={title} aria-current={step === index ? "step" : undefined} className={step === index ? "active" : ""}><span>{index + 1}</span>{title}</li>)}</ol>
    <div className="quick-setup-body">
      {error && <div className="alert error" role="alert">{error}</div>}
      <fieldset disabled={busy} className="config-editor-surface">
        {step === 0 && <section className="page-stack"><h3>仓库托管在哪里？</h3><div className="quick-setup-platforms">{[["github", "GitHub", "github.com 上的仓库"], ["gitlab", "GitLab", "gitlab.com 上的仓库"], ["custom", "自定义", "自建 GitLab / GitHub Enterprise"]].map(([value, title, note]) => <button type="button" key={value} aria-pressed={platform === value} className={`quick-setup-choice ${platform === value ? "selected" : ""}`} onClick={() => choosePlatform(value)}><strong>{title}</strong><small>{note}</small></button>)}</div>
          {platform === "custom" && <div className="form-grid two"><label className="field"><span>平台类型</span><select value={kind} onChange={(event) => { setKind(event.target.value); setSystemVariable(event.target.value === "github" ? "GITHUB_TOKEN" : "GITLAB_TOKEN"); }}><option value="github">GitHub Enterprise</option><option value="gitlab">自建 GitLab</option></select></label><label className="field"><span>平台 API 地址</span><input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder={kind === "github" ? "https://git.example.com/api/v3" : "https://git.example.com/api/v4"} /></label></div>}
          <p className="quick-setup-note">自定义仍需使用 GitHub 或 GitLab API，并非任意 Git 服务。不会修改全局模型或 Agent 的模型设置。</p>
        </section>}
        {step === 1 && <section className="page-stack"><h3>连接仓库</h3>
          <form className="page-stack" autoComplete="off" onSubmit={(event) => event.preventDefault()} aria-label="仓库信息">
          <label className="field"><span>配置目标</span><select value={existing} onChange={(event) => chooseRepository(event.target.value)}><option value="">添加新仓库</option>{props.document.repositories.map((repository) => <option key={repository.id} value={repository.id}>更新：{repository.display_name || repository.id}</option>)}</select></label>
          <div className="form-grid two"><label className="field"><span>仓库 HTTPS / SSH 地址</span><input name="repository-remote" value={remote} onChange={(event) => setRemote(event.target.value)} placeholder={kind === "github" ? "https://github.com/owner/repo.git" : "git@gitlab.com:group/project.git"} /></label><label className="field"><span>显示名称（可选）</span><SetupIntentInput key={`name-${connectionVersion}`} name="repository-display-name" autoComplete="off" value={displayName} onValueChange={setDisplayName} placeholder="留空使用项目名称" /></label></div>
          </form>
          <p className="quick-setup-note">API：{baseUrl}。{existing ? "更新会保留原仓库 ID、本地目录、启停状态和其他设置。" : "仓库 ID 和本地工作目录会自动生成，最后一步可查看。"}</p>
          <label className="field"><span>{tokenName} 凭证来源</span><select value={tokenSource} onChange={(event) => { setTokenSource(event.target.value); setToken(""); setShowToken(false); }}><option value="value">直接填写 Token</option><option value="system">引用宿主机环境变量</option>{existing && <option value="existing">保留当前仓库凭证</option>}</select></label>
          {tokenSource === "value" && <form autoComplete="on" onSubmit={(event) => event.preventDefault()} aria-label="平台凭据">
            {/* 独立账号字段承接密码管理器的用户名，不能把仓库显示名称当成账号。 */}
            <input type="text" name="platform-token-account" autoComplete="username" value={tokenName} readOnly hidden />
            <label className="field"><span>{tokenName}</span><div className="quick-setup-secret"><SetupIntentInput key={`token-${connectionVersion}`} name="platform-access-token" autoComplete="current-password" type={showToken ? "text" : "password"} value={token} onValueChange={setToken} placeholder="点击或开始输入 Token" /><button type="button" className="button secondary" aria-label={showToken ? "隐藏 Token" : "查看 Token 明文"} aria-pressed={showToken} onClick={() => setShowToken(!showToken)}><svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>{showToken && <path d="m3 3 18 18"/>}</svg></button></div><small>固定值会写入本地 config.yaml，请保护配置文件；API 展示、日志与配置历史保持脱敏。避免明文落盘可选择宿主机环境变量。</small></label>
          </form>}
          {tokenSource === "system" && <label className="field"><span>宿主机变量名</span><input value={systemVariable} onChange={(event) => setSystemVariable(event.target.value)} /><small>变量必须存在于启动 Teamwork 服务的环境中，不是当前浏览器所在电脑的环境。</small></label>}
          <p className="quick-setup-note">新填凭证仅用于此仓库：Secret 开启、进程开启、Prompt 关闭。HTTPS Git 使用 Token；SSH 拉取仍需服务账号的 SSH 密钥和已确认的主机。gh / glab 仍需安装在服务端。</p>
        </section>}
        {step === 2 && <section className="page-stack"><h3>这个仓库启用哪些规则？</h3><p className="quick-setup-note">动态列出当前全部事件规则和定时规则。只改变当前仓库的适用关系，保留其他仓库原有行为。</p>
          {!rules.length && <div className="empty-state">当前没有配置规则。可以先接入仓库，之后在“触发规则”页添加。</div>}
          {rules.map((item) => <label className={`quick-setup-rule ${selected.includes(item.key) ? "selected" : ""}`} key={item.key}><input type="checkbox" checked={selected.includes(item.key)} onChange={(event) => setSelected(event.target.checked ? [...selected, item.key] : selected.filter((key) => key !== item.key))} /><div><strong>{item.rule.name}</strong><small>{item.kind === "event" ? "MR / PR 事件" : "定时"} · {setupRuleDescription(item)}</small><small>Agent：{item.rule.agents.join("、") || "未配置"}</small><small>原配置：{item.rule.enabled === false ? "已关闭" : "已启用"} · {item.rule.repositories?.length ? `指定仓库：${item.rule.repositories.join("、")}` : "全部仓库（包含未来新仓库）"}</small></div></label>)}
          <p className="quick-setup-note">原“全部仓库”规则取消勾选后，会改为只包含旧仓库的白名单。原关闭规则勾选后，只为当前仓库开启。没有适用仓库时关闭规则，避免空列表误变成全部。</p>
        </section>}
        {step === 3 && <section className="page-stack"><h3>需要给 Agent 装载 Skill 吗？</h3><label className="quick-setup-rule"><input type="checkbox" checked={useSkills} onChange={(event) => setUseSkills(event.target.checked)} /><div><strong>使用 Skill</strong><small>不勾选时，本仓库禁止装载任何 Skill；不影响其他仓库。</small></div></label>
          {useSkills && <>
            <details><summary>引用新的服务端 Skill 目录</summary><div className="form-grid two"><label className="field"><span>Skill ID</span><input value={skillName} onChange={(event) => setSkillName(event.target.value)} /></label><label className="field"><span>包含 SKILL.md 的服务端目录</span><input value={skillPath} onChange={(event) => setSkillPath(event.target.value)} placeholder="./skills/my-skill" /></label></div><button type="button" className="button secondary" disabled={!skillName.trim() || !skillPath.trim() || skillIds.includes(skillName.trim())} onClick={() => { setNewSkills({ ...newSkills, [skillName.trim()]: skillPath.trim() }); setSkillName(""); setSkillPath(""); }}>加入本次草稿</button><p className="quick-setup-note">这里不复制文件，完成时才注册目录。也可先在 SKILL 页导入完整文件夹，再重新打开向导选择。</p></details>
            {!agents.length && <div className="empty-state">请先返回上一步选择至少一条规则。</div>}
            {Object.entries(newSkills).map(([id, path]) => <div className="quick-setup-agent page-stack" key={id}>
              <label className="field"><span>待注册 Skill：{id}</span><input value={path} onChange={(event) => setNewSkills({ ...newSkills, [id]: event.target.value })} /><small>仅在下方分配给 Agent 后才会注册；可修改目录或移除草稿。</small></label>
              <button type="button" className="button secondary" onClick={() => {
                const remaining = { ...newSkills }; delete remaining[id]; setNewSkills(remaining);
                setAssignments(Object.fromEntries(Object.entries(assignments).map(([name, values]) => [name, values.filter((skill) => skill !== id)])));
              }}>移除待注册 Skill</button>
            </div>)}
            {!skillIds.length && <div className="empty-state">尚无可选 Skill，可引用上面的服务端目录，或暂不使用 Skill。</div>}
            {agents.map((name) => <div className="quick-setup-agent" key={name}><strong>{name}</strong><small>仅覆盖本仓库；包含所选规则允许调用的 sub-agent。</small><div className="quick-setup-skill-options">{skillIds.map((id) => <label key={id}><input type="checkbox" checked={(assignments[name] ?? []).includes(id)} onChange={(event) => setAssignments({ ...assignments, [name]: event.target.checked ? [...(assignments[name] ?? []), id] : (assignments[name] ?? []).filter((skill) => skill !== id) })} />{id}</label>)}</div></div>)}
          </>}
        </section>}
        {step === 4 && summary && <section className="page-stack"><h3>确认配置</h3><dl className="quick-setup-summary"><dt>仓库</dt><dd>{summary.project}</dd><dt>仓库 ID</dt><dd>{summary.repository_id}</dd><dt>本地目录</dt><dd>{summary.workspace}</dd><dt>平台连接</dt><dd>{summary.provider}</dd><dt>凭证变量</dt><dd>{summary.token_env} · {tokenSource === "existing" ? "保留原配置" : "Secret 开 / 进程开 / Prompt 关"}</dd><dt>模型</dt><dd>沿用 Agent 当前配置；未指定时继承全局默认，不作修改。</dd><dt>Skill</dt><dd>{useSkills ? Object.entries(summary.agent_skills).filter(([, ids]) => ids.length).map(([name, ids]) => `${name}：${ids.join("、")}`).join("；") : "本仓库禁止所有 Skill"}</dd></dl>
          <h4>保存后的规则范围</h4>{summary.rules.map((rule) => <div className="quick-setup-rule" key={`${rule.kind}:${rule.name}`}><div><strong>{rule.name} · {rule.applies ? "本仓库启用" : "本仓库不启用"}</strong><small>{!rule.enabled ? "规则关闭，无适用仓库" : rule.repositories.length ? `白名单：${rule.repositories.map((id) => props.document.repositories.find((repo) => repo.id === id)?.display_name || id).join("、")}` : "全部仓库（包括未来新增仓库）"}</small></div></div>)}
          <button type="button" className="button secondary" onClick={() => void checkConnection()}>检查平台与 Git 连接</button>
          {!checks && <p className="quick-setup-note">连接尚未检测。检测只读取仓库和远端引用，不克隆、不发评论；不会测试推送或合并权限，也不启动模型。</p>}
          {checks?.map((check) => <div key={check.name} className={`alert ${check.ok ? "success" : "error"}`}>{check.name}：{check.detail}</div>)}
          {hasFailedCheck && <label className="quick-setup-rule"><input type="checkbox" checked={acceptFailedCheck} onChange={(event) => setAcceptFailedCheck(event.target.checked)} /><span>我了解连接检查未全部通过，仍按所选规则保存，稍后修复运行环境。</span></label>}
          <p className="quick-setup-note">完成后按上面的勾选结果生效，后台可能在下一次扫描或定时周期启动任务。本次保存不额外立即执行 Agent；首次扫描行为沿用已有设置。</p>
        </section>}
      </fieldset>
    </div>
    <footer className="quick-setup-footer"><span>{step + 1} / {steps.length}{busy ? " · 处理中…" : " · 尚未保存"}</span><div className="button-group"><button type="button" className="button secondary" disabled={busy || step === 0} onClick={() => changeStep(step - 1)}>上一步</button>{step < 3 ? <button type="button" className="button primary" disabled={busy || (step === 0 && !baseUrl.trim()) || (step === 1 && !connectionReady)} onClick={() => changeStep(step + 1)}>下一步</button> : step === 3 ? <button type="button" className="button primary" disabled={busy} onClick={() => void preview()}>检查配置</button> : <button type="button" className="button primary" disabled={busy || !summary || Boolean(hasFailedCheck && !acceptFailedCheck)} onClick={() => void complete()}>完成配置</button>}</div></footer>
  </dialog>{confirmExit && <SetupExitConfirmation onContinue={() => setConfirmExit(false)} onDiscard={() => {
    setToken(""); props.onClose();
  }} />}</>;
}
