"""快速配置草稿构造与只读连接检查，不在向导中途持久化配置。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel, Field, SecretStr

from .config import AppConfig, RepositoryConfig
from .environment import resolve_provider_token
from .git_auth import git_credential_context, safe_git_error_detail
from .workspace import WorkspaceError, _run_git


class SetupRequest(BaseModel):
    """向导仅提交当前仓库意图，其他配置始终由后台合并。"""

    revision: str
    kind: Literal["github", "gitlab"]
    base_url: str
    remote: str = Field(min_length=1, max_length=2048)
    existing_repository_id: str | None = None
    display_name: str = ""
    token_source: Literal["value", "system", "existing"] = "value"
    token: SecretStr = Field(default_factory=lambda: SecretStr(""))
    token_system_variable: str = ""
    event_rules: list[str] = Field(default_factory=list)
    scheduled_rules: list[str] = Field(default_factory=list)
    use_skills: bool = False
    agent_skills: dict[str, list[str]] = Field(default_factory=dict)
    new_skills: dict[str, str] = Field(default_factory=dict)


def normalized_connection(request: SetupRequest) -> tuple[str, str]:
    """只接受安全 HTTPS API 与 HTTPS/SSH Git 地址，避免凭据进入 URL。"""

    base_url = request.base_url.strip().rstrip("/")
    remote = request.remote.strip()
    for value in (base_url, remote):
        if any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError("连接地址不能包含空白或控制字符")
    api_url = urlparse(base_url)
    if (
        api_url.scheme != "https" or not api_url.hostname
        or api_url.username or api_url.password or api_url.query or api_url.fragment
    ):
        raise ValueError("平台 API 必须是 HTTPS 地址，不能包含凭据、查询参数或片段")
    if "://" in remote:
        git_url = urlparse(remote)
        if (
            git_url.scheme not in {"https", "ssh"} or not git_url.hostname
            or git_url.password or git_url.query or git_url.fragment
            or (git_url.scheme == "https" and git_url.username)
        ):
            raise ValueError("仓库请使用无密码的 HTTPS 或 SSH 地址；不支持明文 HTTP")
        git_host = git_url.hostname
    else:
        match = re.fullmatch(r"[^/@:]+@([^/:]+):(.+)", remote)
        if not match:
            raise ValueError("请输入完整 HTTPS 或 SSH 仓库地址")
        git_host = match.group(1).lower()
    expected_host = "github.com" if api_url.hostname == "api.github.com" else api_url.hostname
    if git_host != expected_host:
        raise ValueError("仓库与平台 API 主机不匹配，请检查平台类型和自定义 API 地址")
    return base_url, remote


def apply_rule_selection(
    rule: dict[str, Any], repository_id: str, previous_ids: list[str], selected: bool,
) -> dict[str, Any]:
    """保持其他仓库原适用关系；空范围仅代表全部而非没有仓库。"""

    result = copy.deepcopy(rule)
    enabled = rule.get("enabled", True)
    scope = rule.get("repositories") or []
    if not enabled:
        if selected:
            result.update(enabled=True, repositories=[repository_id])
        return result
    if not scope and selected:
        return result
    targets = list(scope or previous_ids)
    targets = [item for item in targets if item != repository_id]
    if selected:
        targets.append(repository_id)
    targets = list(dict.fromkeys(targets))
    result.update(repositories=targets, enabled=bool(targets))
    return result


def setup_agent_names(document: dict[str, Any], request: SetupRequest) -> set[str]:
    """按真实规则引用展开 sub-agent，循环引用不导致无限遍历。"""

    pending = [
        name
        for key, chosen in (("rules", request.event_rules), ("scheduled_rules", request.scheduled_rules))
        for rule in document.get(key, []) if rule["name"] in chosen
        for name in rule.get("agents", [])
    ]
    names: set[str] = set()
    while pending:
        name = pending.pop()
        if name in names:
            continue
        names.add(name)
        pending.extend(document.get("agents", {}).get(name, {}).get("allowed_sub_agents", []))
    return names


def build_setup_document(
    current: dict[str, Any], request: SetupRequest, config_path: Path,
) -> tuple[dict[str, Any], str]:
    """构造一次完整原子保存的候选配置，不创建目录或改变运行状态。"""

    base_url, remote = normalized_connection(request)
    project = RepositoryConfig(
        id="draft", provider="draft", project=remote, workspace=".",
    ).project
    if request.kind == "github" and len(project.split("/")) != 2:
        raise ValueError("GitHub 项目路径必须是 owner/repository")
    document = copy.deepcopy(current)
    providers = document.setdefault("providers", {})
    repositories = document.setdefault("repositories", [])
    previous_ids = [item["id"] for item in repositories]
    existing = next((item for item in repositories if item["id"] == request.existing_repository_id), None)
    if request.existing_repository_id and existing is None:
        raise ValueError("要更新的仓库已不存在，请重新打开向导")
    duplicates = [
        item for item in repositories
        if providers[item["provider"]]["kind"] == request.kind
        and providers[item["provider"]]["base_url"].rstrip("/") == base_url
        and RepositoryConfig.model_validate(item).project.casefold() == project.casefold()
    ]
    if any(item is not existing for item in duplicates):
        raise ValueError("该平台项目已配置，请在第二步选择更新已有仓库")
    if existing:
        provider = providers[existing["provider"]]
        if (
            provider["kind"] != request.kind or provider["base_url"].rstrip("/") != base_url
            or RepositoryConfig.model_validate(existing).project != project
        ):
            raise ValueError("向导更新不改变平台或项目身份，请到仓库详情修改")
        repository = existing
        repository_id = existing["id"]
    else:
        suffix = hashlib.sha256(f"{request.kind}:{base_url}:{project}".encode()).hexdigest()[:8]
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", project).strip("-")[:60] or "repository"
        repository_id = f"{slug}-{suffix}"
        if repository_id in previous_ids:
            raise ValueError("仓库 ID 已存在，请通过仓库详情管理")
        workspace = f"./workspaces/{repository_id}"
        resolved_workspace = (config_path.parent / workspace).resolve()
        existing_workspaces = [(config_path.parent / item["workspace"]).resolve() for item in repositories]
        if resolved_workspace.exists() or any(
            resolved_workspace == path or path in resolved_workspace.parents or resolved_workspace in path.parents
            for path in existing_workspaces
        ):
            raise ValueError("自动生成的工作目录已被占用，请通过仓库详情指定目录")
        provider_id = next((name for name, value in providers.items()
                            if value["kind"] == request.kind and value["base_url"].rstrip("/") == base_url), None)
        if provider_id is None:
            provider_id = f"{request.kind}-{hashlib.sha256(base_url.encode()).hexdigest()[:8]}"
            if provider_id in providers:
                raise ValueError("自动生成的平台连接 ID 已被占用")
            providers[provider_id] = {
                "kind": request.kind, "base_url": base_url,
                "token_env": "GITHUB_TOKEN" if request.kind == "github" else "GITLAB_TOKEN",
            }
        repository = {"id": repository_id, "provider": provider_id, "workspace": workspace, "enabled": True}
        repositories.append(repository)
    repository.update(project=project, clone_url=remote, display_name=request.display_name.strip() or project)
    token_name = providers[repository["provider"]]["token_env"]
    if request.token_source == "existing":
        if not existing:
            raise ValueError("新仓库需要填写 Token 或引用宿主机环境变量")
    else:
        definition: dict[str, Any] = {"secret": True, "expose_to_prompt": False, "expose_to_process": True}
        if request.token_source == "value":
            token = request.token.get_secret_value().strip()
            if not token or token == "********" or any(char.isspace() for char in token):
                raise ValueError("请填写有效 Token，不能使用脱敏占位符")
            definition["value"] = token
        else:
            name = request.token_system_variable.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("宿主机 Token 环境变量名无效")
            definition["from_system"] = name
        repository.setdefault("environment", {})[token_name] = definition
    for key, selected in (("rules", request.event_rules), ("scheduled_rules", request.scheduled_rules)):
        rules = document.get(key, [])
        if set(selected) - {rule["name"] for rule in rules}:
            raise ValueError("所选规则已不存在，请重新打开向导")
        document[key] = [apply_rule_selection(rule, repository_id, previous_ids, rule["name"] in selected) for rule in rules]
    if request.use_skills:
        agents = setup_agent_names(document, request)
        if set(request.agent_skills) - agents:
            raise ValueError("Skill 只能分配给所选规则及其 sub-agent")
        assignments = {name: list(dict.fromkeys(request.agent_skills.get(name, []))) for name in agents}
        selected_skills = sorted({skill for items in assignments.values() for skill in items})
        if not selected_skills:
            raise ValueError("使用 Skill 时请至少为一个 Agent 选择 Skill")
        skills = document.setdefault("skills", {})
        for name, path in request.new_skills.items():
            # 草稿中试填但最终未分配的目录不注册，也不影响最终配置校验。
            if name not in selected_skills:
                continue
            if not name.strip() or not path.strip():
                raise ValueError("新增 Skill 需要名称和服务端目录")
            if name in skills and skills[name]["path"] != path:
                raise ValueError("Skill 名称已存在，不能覆盖其他仓库使用的 Skill")
            skills[name] = {"path": path}
        repository.update(allowed_skills=selected_skills, agent_skills=assignments)
    else:
        repository.update(allowed_skills=None, agent_skills={})
    return document, repository_id


def setup_summary(config: AppConfig, repository_id: str) -> dict[str, Any]:
    """预览只返回非秘密配置，不能返回候选文档或 Token 值。"""

    repository = config.repository_map()[repository_id]
    return {
        "repository_id": repository_id, "workspace": str(repository.workspace),
        "provider": repository.provider, "project": repository.project,
        "token_env": config.providers[repository.provider].token_env,
        "rules": [
            {"name": rule.name, "kind": kind, "enabled": rule.enabled,
             "repositories": rule.repositories or [],
             "applies": rule.enabled and (not rule.repositories or repository_id in rule.repositories)}
            for kind, rules in (("event", config.rules), ("scheduled", config.scheduled_rules)) for rule in rules
        ],
        "agent_skills": repository.agent_skills,
    }


async def check_setup_connection(config: AppConfig, repository_id: str) -> list[dict[str, Any]]:
    """只读检测 API 和 Git，不读取 diff、不运行 Agent、不测试远端写入。"""

    repository = config.repository_map()[repository_id]
    provider = config.providers[repository.provider]
    token = resolve_provider_token(config, provider, repository=repository)
    if not token:
        return [{"name": "平台 API", "ok": False, "detail": "未解析到 Token，请检查宿主机变量或仓库凭据"}]
    path = f"repos/{quote(repository.project, safe='/')}" if provider.kind == "github" else f"projects/{quote(repository.project, safe='')}"
    headers = {"Authorization": f"Bearer {token}"} if provider.kind == "github" else {"PRIVATE-TOKEN": token}
    results: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(f"{provider.base_url.rstrip('/')}/{path}", headers=headers)
        ok = response.status_code == 200 and isinstance(response.json(), dict) and bool(response.json().get("id"))
        results.append({"name": "平台 API", "ok": ok, "detail": "仓库读取成功（不代表写权限）" if ok else f"仓库读取失败：HTTP {response.status_code}"})
    except Exception:
        # 网络及上游异常不可直接展示，防止响应、代理地址或请求头泄露凭据。
        results.append({"name": "平台 API", "ok": False, "detail": "无法读取平台仓库，请检查 API 地址、Token、代理与 TLS"})

    def check_git() -> bool:
        """使用短超时及非交互 SSH，避免向导被密码或主机确认阻塞。"""

        with git_credential_context(token, provider_kind=provider.kind) as credentials:
            credentials.environment["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8"
            _run_git(["ls-remote", "--", str(repository.clone_url), "HEAD"], timeout_seconds=15)
        return True
    try:
        await asyncio.to_thread(check_git)
        results.append({"name": "Git", "ok": True, "detail": "远端读取成功（未克隆，未验证推送权限）"})
    except WorkspaceError as exc:
        # 已知 Git 故障保留排障信息；再次脱敏后才交给界面，禁止返回原始 stderr。
        detail = safe_git_error_detail(str(exc), secrets=(token,)) or "Git 执行失败，未返回错误详情"
        results.append({"name": "Git", "ok": False, "detail": detail})
    except Exception:
        results.append({"name": "Git", "ok": False, "detail": "Git 读取失败：请检查服务账号的网络、HTTPS Token 或 SSH 密钥及 known_hosts"})
    return results
