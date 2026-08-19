"""FastAPI 管理 API、SSE 日志和 React 静态资源入口。"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .codex_account import (
    CodexAccountError,
    CodexLoginManager,
    inspect_codex_account,
    read_codex_effective_config,
)
from .config_manager import ConfigManager, ConfigRevisionConflict
from .codex_settings import codex_home, inspect_runtime_options
from .environment import PromptRenderError, render_prompt
from .events import (
    FIELD_EVENTS,
    TARGET_COMMITS_CHANGED_EVENT,
    create_manual_activity_event,
    detect_events,
)
from .prompt_files import MAX_PROMPT_FILE_BYTES, import_prompt_file, list_prompt_files
from .repository_initialization import RepositoryInitializationManager
from .runtime import BackgroundRuntime
from .skill_files import (
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_FILES,
    MAX_SKILL_TOTAL_BYTES,
    import_skill_directory,
    inspect_skill_path,
    list_skill_directories,
)


class ConfigDocumentRequest(BaseModel):
    """UI 提交的完整配置文档。"""

    document: dict[str, Any]


class AgentConfigRequest(BaseModel):
    """UI 提交的单个 Agent 配置。"""

    revision: str
    name: str
    agent: dict[str, Any]


class AgentDeleteRequest(BaseModel):
    """UI 删除单个 Agent 时提交的配置版本。"""

    revision: str


class RuleConfigRequest(BaseModel):
    """UI 提交的单条触发规则配置。"""

    revision: str
    name: str
    rule: dict[str, Any]


class RuleDeleteRequest(BaseModel):
    """UI 删除单条触发规则时提交的配置版本。"""

    revision: str


class ProviderConfigRequest(BaseModel):
    """UI 提交的单个平台连接配置。"""

    revision: str
    name: str
    provider: dict[str, Any]


class ProviderDeleteRequest(BaseModel):
    """UI 删除单个平台连接时提交的配置版本。"""

    revision: str


class RepositoryConfigRequest(BaseModel):
    """UI 提交的单个仓库配置。"""

    revision: str
    repository_id: str
    repository: dict[str, Any]


class RepositoryDeleteRequest(BaseModel):
    """UI 删除单个仓库时提交的配置版本。"""

    revision: str


class ManualLatestEventTarget(BaseModel):
    """批量手动触发中的单个 MR / PR 目标。"""

    repository_id: str = Field(min_length=1)
    number: int = Field(ge=1)


class ManualLatestEventBatchRequest(BaseModel):
    """批量手动触发最新事件请求。"""

    targets: list[ManualLatestEventTarget] = Field(min_length=1, max_length=100)


class PromptPreviewRequest(BaseModel):
    """Prompt 模板的纯文本预览请求。"""

    template: str
    variables: dict[str, str] = Field(default_factory=dict)


class SkillInspectRequest(BaseModel):
    """服务端 Skill 目录检查请求。"""

    path: str


def create_app(
    config_path: str | Path,
    *,
    start_scheduler: bool = True,
) -> FastAPI:
    """创建可测试的后台应用实例。"""

    manager = ConfigManager(config_path)
    runtime = BackgroundRuntime(manager)
    login_manager = CodexLoginManager()
    repository_initialization_manager = RepositoryInitializationManager(manager)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if start_scheduler:
            await runtime.start()
        try:
            yield
        finally:
            await repository_initialization_manager.close()
            await login_manager.close()
            if start_scheduler:
                await runtime.stop()

    app = FastAPI(
        title="Teamwork Review Agents",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.config_manager = manager
    app.state.runtime = runtime
    app.state.codex_login_manager = login_manager
    app.state.repository_initialization_manager = repository_initialization_manager

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        """配置管理员 Token 后保护全部管理 API。"""

        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            token_env = manager.config.web.admin_token_env
            expected = os.getenv(token_env, "") if token_env else ""
            if token_env:
                authorization = request.headers.get("Authorization", "")
                bearer = authorization.removeprefix("Bearer ").strip()
                supplied = request.headers.get("X-Admin-Token", "") or bearer
                if not expected or supplied != expected:
                    return JSONResponse({"detail": "管理员认证失败"}, status_code=401)
        return await call_next(request)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": app.version, "pid": os.getpid()}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return await runtime.snapshot()

    @app.post("/api/control/scan")
    async def scan_now() -> dict[str, Any]:
        runtime.scan_now()
        return {"accepted": True}

    @app.post("/api/control/pause")
    async def pause() -> dict[str, Any]:
        runtime.pause()
        return {"paused": True}

    @app.post("/api/control/resume")
    async def resume() -> dict[str, Any]:
        runtime.resume()
        return {"paused": False}

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return {
            "revision": manager.config.revision,
            "document": await asyncio.to_thread(manager.document),
            "error": manager.last_error,
        }

    @app.post("/api/config/validate")
    async def validate_config(body: ConfigDocumentRequest) -> dict[str, Any]:
        try:
            config = await asyncio.to_thread(manager.validate, body.document)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"valid": True, "revision": config.revision}

    @app.put("/api/config")
    async def save_config(body: ConfigDocumentRequest) -> dict[str, Any]:
        try:
            config = await asyncio.to_thread(
                manager.save,
                body.document,
                source="ui",
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.notify_config_changed()
        return {
            "revision": config.revision,
            "document": await asyncio.to_thread(manager.document),
        }

    async def item_config_response(config_revision: str) -> dict[str, Any]:
        """返回单项操作后的最新完整配置视图。"""

        return {
            "revision": config_revision,
            "document": await asyncio.to_thread(manager.document),
        }

    @app.post("/api/config/agents")
    async def create_agent(body: AgentConfigRequest) -> dict[str, Any]:
        """基于指定版本创建一个 Agent。"""

        try:
            config = await asyncio.to_thread(
                manager.save_agent,
                expected_revision=body.revision,
                name=body.name,
                agent=body.agent,
                source="ui-agent-create",
            )
        except ConfigRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.notify_config_changed()
        return await item_config_response(config.revision)

    @app.put("/api/config/agents/{agent_name:path}")
    async def update_agent(
        agent_name: str,
        body: AgentConfigRequest,
    ) -> dict[str, Any]:
        """基于指定版本更新或重命名一个 Agent。"""

        try:
            config = await asyncio.to_thread(
                manager.save_agent,
                expected_revision=body.revision,
                original_name=agent_name,
                name=body.name,
                agent=body.agent,
                source="ui-agent-update",
            )
        except ConfigRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.notify_config_changed()
        return await item_config_response(config.revision)

    @app.delete("/api/config/agents/{agent_name:path}")
    async def delete_agent(
        agent_name: str,
        body: AgentDeleteRequest,
    ) -> dict[str, Any]:
        """基于指定版本删除一个 Agent。"""

        try:
            config = await asyncio.to_thread(
                manager.delete_agent,
                expected_revision=body.revision,
                name=agent_name,
            )
        except ConfigRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.notify_config_changed()
        return await item_config_response(config.revision)

    @app.post("/api/config/rules")
    async def create_rule(body: RuleConfigRequest) -> dict[str, Any]:
        """基于指定版本创建一条触发规则。"""

        try:
            config = await asyncio.to_thread(
                manager.save_rule,
                expected_revision=body.revision,
                name=body.name,
                rule=body.rule,
                source="ui-rule-create",
            )
        except ConfigRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.notify_config_changed()
        return await item_config_response(config.revision)

    @app.put("/api/config/rules/{rule_name:path}")
    async def update_rule(
        rule_name: str,
        body: RuleConfigRequest,
    ) -> dict[str, Any]:
        """基于指定版本更新或重命名一条触发规则。"""

        try:
            config = await asyncio.to_thread(
                manager.save_rule,
                expected_revision=body.revision,
                original_name=rule_name,
                name=body.name,
                rule=body.rule,
                source="ui-rule-update",
            )
        except ConfigRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.notify_config_changed()
        return await item_config_response(config.revision)

    @app.delete("/api/config/rules/{rule_name:path}")
    async def delete_rule(
        rule_name: str,
        body: RuleDeleteRequest,
    ) -> dict[str, Any]:
        """基于指定版本删除一条触发规则。"""

        try:
            config = await asyncio.to_thread(
                manager.delete_rule,
                expected_revision=body.revision,
                name=rule_name,
            )
        except ConfigRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.notify_config_changed()
        return await item_config_response(config.revision)

    async def ensure_repository_idle(repository_id: str) -> None:
        """禁止在基础仓库或 Agent 正在准备工作区时修改目标配置。"""

        status = await repository_initialization_manager.get(repository_id)
        if status is not None and status.get("status") in {
            "waiting",
            "initializing",
            "updating",
        }:
            raise HTTPException(
                status_code=409,
                detail="仓库正在执行 Git 操作，请等待完成或先取消操作",
            )

    @app.post("/api/config/providers")
    async def create_provider(body: ProviderConfigRequest) -> dict[str, Any]:
        """基于指定版本创建一个平台连接。"""

        try:
            config = await asyncio.to_thread(
                manager.save_provider,
                expected_revision=body.revision,
                name=body.name,
                provider=body.provider,
                source="ui-provider-create",
            )
        except ConfigRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.notify_config_changed()
        return await item_config_response(config.revision)

    @app.put("/api/config/providers/{provider_name:path}")
    async def update_provider(
        provider_name: str,
        body: ProviderConfigRequest,
    ) -> dict[str, Any]:
        """基于指定版本更新或重命名一个平台连接。"""

        try:
            config = await asyncio.to_thread(
                manager.save_provider,
                expected_revision=body.revision,
                original_name=provider_name,
                name=body.name,
                provider=body.provider,
                source="ui-provider-update",
            )
        except ConfigRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.notify_config_changed()
        return await item_config_response(config.revision)

    @app.delete("/api/config/providers/{provider_name:path}")
    async def delete_provider(
        provider_name: str,
        body: ProviderDeleteRequest,
    ) -> dict[str, Any]:
        """基于指定版本安全删除一个平台连接。"""

        try:
            config = await asyncio.to_thread(
                manager.delete_provider,
                expected_revision=body.revision,
                name=provider_name,
            )
        except ConfigRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.notify_config_changed()
        return await item_config_response(config.revision)

    @app.post("/api/config/repositories")
    async def create_repository(body: RepositoryConfigRequest) -> dict[str, Any]:
        """基于指定版本创建一个仓库。"""

        try:
            config = await asyncio.to_thread(
                manager.save_repository,
                expected_revision=body.revision,
                repository_id=body.repository_id,
                repository=body.repository,
                source="ui-repository-create",
            )
        except ConfigRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.notify_config_changed()
        return await item_config_response(config.revision)

    @app.put("/api/config/repositories/{repository_id:path}")
    async def update_repository(
        repository_id: str,
        body: RepositoryConfigRequest,
    ) -> dict[str, Any]:
        """基于指定版本更新一个仓库，仓库 ID 保持不变。"""

        await ensure_repository_idle(repository_id)
        try:
            config = await asyncio.to_thread(
                manager.save_repository,
                expected_revision=body.revision,
                original_id=repository_id,
                repository_id=body.repository_id,
                repository=body.repository,
                source="ui-repository-update",
            )
        except ConfigRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.notify_config_changed()
        return await item_config_response(config.revision)

    @app.delete("/api/config/repositories/{repository_id:path}")
    async def delete_repository(
        repository_id: str,
        body: RepositoryDeleteRequest,
    ) -> dict[str, Any]:
        """基于指定版本安全删除一个仓库配置。"""

        await ensure_repository_idle(repository_id)
        try:
            config = await asyncio.to_thread(
                manager.delete_repository,
                expected_revision=body.revision,
                repository_id=repository_id,
            )
        except ConfigRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.notify_config_changed()
        return await item_config_response(config.revision)

    @app.get("/api/config/versions")
    async def config_versions(limit: int = Query(default=20, ge=1, le=100)):
        return await asyncio.to_thread(manager.store.list_config_versions, limit)

    @app.get("/api/config/versions/{revision}")
    async def config_version(revision: str):
        version = await asyncio.to_thread(manager.store.get_config_version, revision)
        if version is None:
            raise HTTPException(status_code=404, detail="配置版本不存在")
        return version

    @app.get("/api/options")
    async def options() -> dict[str, Any]:
        return {
            "events": [
                "change_request.discovered",
                "change_request.opened",
                "change_request.reopened",
                "change_request.closed",
                "change_request.merged",
                *sorted(
                    set(FIELD_EVENTS.values()) | {TARGET_COMMITS_CHANGED_EVENT}
                ),
                "change_request.updated",
            ],
            "operators": [
                "eq",
                "ne",
                "contains",
                "not_contains",
                "in",
                "not_in",
                "gte",
                "lte",
                "gt",
                "lt",
                "changed",
            ],
        }

    @app.get("/api/repositories/workspaces")
    async def repository_workspaces() -> list[dict[str, Any]]:
        """返回已保存仓库的基础 Git 目录状态。"""

        return await repository_initialization_manager.list()

    @app.get("/api/repositories/{repository_id}/workspace")
    async def repository_workspace(repository_id: str) -> dict[str, Any]:
        """返回一个已保存仓库的基础 Git 目录状态。"""

        result = await repository_initialization_manager.get(repository_id)
        if result is None:
            raise HTTPException(status_code=404, detail="仓库配置不存在")
        return result

    @app.get("/api/repositories/{repository_id}/workspace/details")
    async def repository_workspace_details(repository_id: str) -> dict[str, Any]:
        """返回最近一次基础仓库或 Agent Git 操作的脱敏详情。"""

        result = await repository_initialization_manager.detail(repository_id)
        if result is None:
            raise HTTPException(status_code=404, detail="仓库配置不存在")
        return result

    @app.post("/api/repositories/{repository_id}/workspace/initialize")
    async def initialize_repository(repository_id: str) -> dict[str, Any]:
        """启动基础仓库初始化或增量更新。"""

        try:
            result = await repository_initialization_manager.start(repository_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="仓库配置不存在")
        return result

    @app.post("/api/repositories/{repository_id}/workspace/cancel")
    async def cancel_repository_initialization(repository_id: str) -> dict[str, Any]:
        """取消仍在进行的基础仓库初始化或更新。"""

        result = await repository_initialization_manager.cancel(repository_id)
        if result is None:
            raise HTTPException(status_code=404, detail="仓库配置不存在")
        return result

    @app.get("/api/codex/runtime-options")
    async def codex_runtime_options() -> dict[str, Any]:
        """返回本机 Codex 模型目录和当前可验证的继承模型来源。"""

        effective_config = None
        effective_config_error = None
        try:
            effective_config = await read_codex_effective_config(
                manager.config.runtime.codex_binary,
                codex_home(manager.config.runtime.codex_home),
            )
        except (CodexAccountError, OSError) as exc:
            effective_config_error = str(exc)

        return await asyncio.to_thread(
            inspect_runtime_options,
            manager.config.runtime.codex,
            manager.config.runtime.codex_binary,
            manager.config.runtime.codex_home,
            manager.config.runtime.expected_codex_version,
            effective_config,
            effective_config_error,
            manager.config.runtime.managed_sandbox,
        )

    @app.get("/api/codex/account")
    async def codex_account() -> dict[str, Any]:
        """返回已保存独立 Codex Home 的脱敏账户和额度信息。"""

        try:
            return await inspect_codex_account(
                manager.config.runtime.codex_binary,
                manager.config.runtime.codex_home,
            )
        except (CodexAccountError, OSError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/codex/login")
    async def start_codex_login() -> dict[str, Any]:
        """为已保存的独立 Codex Home 启动 ChatGPT 浏览器登录。"""

        home = manager.config.runtime.codex_home
        if home is None:
            raise HTTPException(
                status_code=409,
                detail="请先配置并保存独立的后台 Codex Home",
            )
        try:
            return await login_manager.start(
                manager.config.runtime.codex_binary,
                home,
            )
        except (CodexAccountError, OSError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/codex/login/{session_id}")
    async def codex_login_status(session_id: str) -> dict[str, Any]:
        """返回登录会话状态，不包含任何认证凭据。"""

        session = login_manager.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Codex 登录会话不存在")
        return session

    @app.post("/api/codex/login/{session_id}/cancel")
    async def cancel_codex_login(session_id: str) -> dict[str, Any]:
        """取消仍在等待浏览器授权的 Codex 登录。"""

        session = await login_manager.cancel(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Codex 登录会话不存在")
        return session

    @app.post("/api/prompts/preview")
    async def preview_prompt(body: PromptPreviewRequest) -> dict[str, str]:
        """按运行时相同的沙盒 Jinja 规则预览 Prompt。"""

        try:
            rendered = render_prompt(body.template, body.variables)
        except PromptRenderError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"rendered": rendered}

    @app.get("/api/prompt-files")
    async def prompt_files() -> list[dict[str, Any]]:
        """列出配置目录下已经导入的 Prompt 文件。"""

        return await asyncio.to_thread(list_prompt_files, manager.path)

    @app.post("/api/prompt-files/import")
    async def upload_prompt_file(file: UploadFile = File(...)) -> dict[str, Any]:
        """将浏览器选择的文本文件复制到配置旁的 prompts 目录。"""

        filename = file.filename or ""
        try:
            content = await file.read(MAX_PROMPT_FILE_BYTES + 1)
            if len(content) > MAX_PROMPT_FILE_BYTES:
                raise HTTPException(status_code=413, detail="Prompt 文件不能超过 1 MiB")
            return await asyncio.to_thread(
                import_prompt_file,
                manager.path,
                filename,
                content,
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            await file.close()

    @app.get("/api/skill-directories")
    async def skill_directories() -> list[dict[str, Any]]:
        """列出配置目录旁已经导入的 Skill 文件夹。"""

        return await asyncio.to_thread(list_skill_directories, manager.path)

    @app.post("/api/skill-directories/inspect")
    async def inspect_skill(body: SkillInspectRequest) -> dict[str, Any]:
        """检查服务端已有 Skill 路径并读取展示元数据。"""

        try:
            return await asyncio.to_thread(inspect_skill_path, manager.path, body.path)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/skill-directories/import")
    async def upload_skill_directory(
        files: list[UploadFile] = File(...),
    ) -> dict[str, Any]:
        """将浏览器选择的完整 Skill 文件夹复制到配置旁的 skills 目录。"""

        if len(files) > MAX_SKILL_FILES:
            for file in files:
                await file.close()
            raise HTTPException(
                status_code=413,
                detail=f"单个 Skill 最多包含 {MAX_SKILL_FILES} 个文件",
            )
        uploaded: list[tuple[str, bytes]] = []
        total_bytes = 0
        try:
            for file in files:
                content = await file.read(MAX_SKILL_FILE_BYTES + 1)
                if len(content) > MAX_SKILL_FILE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Skill 单个文件不能超过 8 MiB：{file.filename or ''}",
                    )
                total_bytes += len(content)
                if total_bytes > MAX_SKILL_TOTAL_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="单个 Skill 全部文件合计不能超过 32 MiB",
                    )
                uploaded.append((file.filename or "", content))
            return await asyncio.to_thread(
                import_skill_directory,
                manager.path,
                uploaded,
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            for file in files:
                await file.close()

    @app.get("/api/runs")
    async def runs(
        limit: int = Query(default=50, ge=1, le=500),
        status: str | None = None,
        agent_name: str | None = None,
        repository_id: str | None = None,
    ):
        return await asyncio.to_thread(
            manager.store.list_runs,
            limit,
            status=status,
            agent_name=agent_name,
            repository_id=repository_id,
        )

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str):
        run = await asyncio.to_thread(manager.store.get_run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="运行记录不存在")
        return run

    @app.get("/api/preflight-runs")
    async def preflight_runs(
        limit: int = Query(default=100, ge=1, le=500),
        status: Literal[
            "running",
            "success",
            "failure",
            "timed_out",
            "error",
        ]
        | None = None,
        repository_id: str | None = None,
        number: int | None = Query(default=None, ge=1),
    ):
        """返回本地 Preflight / CI 运行摘要，不包含完整输出。"""

        return await asyncio.to_thread(
            manager.store.list_preflight_runs,
            limit,
            status=status,
            repository_id=repository_id,
            number=number,
        )

    @app.get("/api/preflight-runs/{run_id}")
    async def preflight_run_detail(run_id: str):
        """按需返回本地 Preflight / CI 完整结果。"""

        run = await asyncio.to_thread(manager.store.get_preflight_run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="本地 CI 运行记录不存在")
        return run

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        """持久化取消指定运行及其全部后代。"""

        run_ids = await asyncio.to_thread(manager.store.request_cancel_run, run_id)
        if run_ids is None:
            raise HTTPException(status_code=404, detail="运行记录不存在")
        if not run_ids:
            return {"accepted": False, "run_ids": [], "reason": "运行已经结束"}
        for active_run_id in run_ids:
            await asyncio.to_thread(
                manager.store.append_run_log,
                active_run_id,
                stream="system",
                event_type="run.cancel_requested",
                payload="管理员已请求取消运行",
            )
        return {
            "accepted": True,
            "run_ids": run_ids,
            "reason": f"已请求取消 {len(run_ids)} 个运行",
        }

    @app.get("/api/runs/{run_id}/logs")
    async def run_logs(
        run_id: str,
        after_id: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=2000),
    ):
        return await asyncio.to_thread(
            manager.store.list_run_logs,
            run_id,
            after_id=after_id,
            limit=limit,
        )

    @app.get("/api/runs/{run_id}/stream")
    async def stream_run_logs(
        run_id: str,
        after_id: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        if await asyncio.to_thread(manager.store.get_run, run_id) is None:
            raise HTTPException(status_code=404, detail="运行记录不存在")

        async def generate() -> AsyncIterator[str]:
            cursor = after_id
            idle_after_terminal = 0
            heartbeat_ticks = 0
            while True:
                logs = await asyncio.to_thread(
                    manager.store.list_run_logs,
                    run_id,
                    after_id=cursor,
                    limit=500,
                )
                for log in logs:
                    cursor = int(log["id"])
                    heartbeat_ticks = 0
                    yield (
                        f"id: {cursor}\n"
                        f"event: {log['event_type']}\n"
                        f"data: {json.dumps(log, ensure_ascii=False)}\n\n"
                    )
                run = await asyncio.to_thread(manager.store.get_run, run_id)
                terminal = run and run.get("status") in {
                    "completed",
                    "failed",
                    "timed_out",
                    "cancelled",
                }
                if terminal and not logs:
                    idle_after_terminal += 1
                    if idle_after_terminal >= 2:
                        yield "event: end\ndata: {}\n\n"
                        return
                else:
                    idle_after_terminal = 0
                if not logs and not terminal:
                    heartbeat_ticks += 1
                    if heartbeat_ticks >= 30:
                        # 定期注释帧避免反向代理关闭安静但仍在运行的日志流。
                        yield ": heartbeat\n\n"
                        heartbeat_ticks = 0
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/events")
    async def events(
        limit: int = Query(default=50, ge=1),
        all_records: bool = False,
        repository_id: str | None = None,
        number: int | None = Query(default=None, ge=1),
        status: Literal[
            "pending",
            "processing",
            "unmatched",
            "triggered",
            "completed",
            "failed",
            "cancelled",
        ]
        | None = None,
    ):
        return await asyncio.to_thread(
            manager.store.list_events,
            None if all_records else limit,
            status=status,
            repository_id=repository_id,
            number=number,
        )

    @app.get("/api/events/{event_id}")
    async def event_detail(event_id: str):
        """按需返回事件处理、Agent 调度和本地 CI 详情。"""

        detail = await asyncio.to_thread(manager.store.get_event_detail, event_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        return detail

    @app.get("/api/change-requests")
    async def change_requests(
        limit: int = Query(default=100, ge=1),
        all_records: bool = False,
        repository_id: str | None = None,
        status: Literal["opened", "closed", "merged"] | None = None,
    ):
        """返回扫描器已经建立基线的 MR/PR 最新快照。"""

        records = await asyncio.to_thread(
            manager.store.list_snapshots,
            None if all_records else limit,
            repository_id=repository_id,
            status=status,
        )
        provider_map = manager.config.providers
        for record in records:
            provider = provider_map.get(str(record.get("provider") or ""))
            record["latest_event_supported"] = bool(
                provider is not None and provider.kind == "github"
            )
        return records

    @app.get("/api/change-request-detail")
    async def change_request_detail(
        repository_id: str,
        number: int = Query(ge=1),
    ):
        """按需返回 MR/PR 当前快照与关联事件摘要。"""

        detail = await asyncio.to_thread(
            manager.store.get_change_request_detail,
            repository_id,
            number,
        )
        if detail is None:
            raise HTTPException(status_code=404, detail="MR/PR 快照不存在")
        provider = manager.config.providers.get(str(detail.get("provider") or ""))
        detail["latest_event_supported"] = bool(
            provider is not None and provider.kind == "github"
        )
        return detail

    @app.post("/api/change-requests/{repository_id}/{number}/emit-discovered")
    async def emit_discovered(repository_id: str, number: int):
        """为已有快照幂等补发首次发现事件，并唤醒后台处理规则。"""

        snapshot = await asyncio.to_thread(
            manager.store.load_snapshot,
            f"{repository_id}:{number}",
        )
        if snapshot is None:
            raise HTTPException(status_code=404, detail="MR / PR 快照不存在，请先执行扫描")

        event_type = "change_request.discovered"
        already_emitted = await asyncio.to_thread(
            manager.store.has_event_type,
            repository_id,
            number,
            event_type,
        )
        if already_emitted:
            return {"created": False, "reason": "首次发现事件已经存在"}

        event = detect_events(None, snapshot, emit_initial=True)[0]
        inserted = await asyncio.to_thread(manager.store.enqueue_events, [event])
        if inserted:
            runtime.dispatch_events_now()
        return {
            "created": bool(inserted),
            "event_id": event.id,
            "reason": "首次发现事件已补发" if inserted else "首次发现事件已经存在",
        }

    async def create_latest_manual_event(repository_id: str, number: int):
        """校验目标并创建尚未写入数据库的最新手动事件。"""
        snapshot = await asyncio.to_thread(
            manager.store.load_snapshot,
            f"{repository_id}:{number}",
        )
        if snapshot is None:
            raise HTTPException(status_code=404, detail="MR / PR 快照不存在，请先执行扫描")
        repository = manager.config.repository_map().get(repository_id)
        if repository is None or not repository.enabled:
            raise HTTPException(status_code=409, detail="仓库未启用，不能手动触发事件")
        activity = await asyncio.to_thread(
            manager.store.load_latest_activity,
            snapshot.provider,
            repository_id,
            number,
        )
        if activity is None:
            raise HTTPException(
                status_code=409,
                detail="尚未取得可触发的最新 Provider 事件，请先完成一次扫描",
            )
        event = create_manual_activity_event(snapshot, activity)
        return event, activity

    @app.post(
        "/api/change-requests/{repository_id}/{number}/trigger-latest-event"
    )
    async def trigger_latest_event(repository_id: str, number: int):
        """把已缓存的最新 Provider 活动作为新的手动事件送入规则引擎。"""

        event, activity = await create_latest_manual_event(repository_id, number)
        inserted = await asyncio.to_thread(manager.store.enqueue_events, [event])
        if not inserted:
            raise HTTPException(status_code=409, detail="手动事件未能写入，请重新操作")
        runtime.dispatch_events_now()
        return {
            "created": True,
            "event_id": event.id,
            "event_type": event.type,
            "source_activity_id": activity.id,
            "source_occurred_at": (
                activity.occurred_at.isoformat()
                if activity.occurred_at is not None
                else None
            ),
            "reason": f"已手动发送 {event.type}",
        }

    @app.post("/api/change-requests/trigger-latest-events")
    async def trigger_latest_events(request: ManualLatestEventBatchRequest):
        """逐项创建最新手动事件，并让单项失败不阻塞其他目标。"""

        results: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        created_count = 0
        for target in request.targets:
            key = (target.repository_id, target.number)
            if key in seen:
                results.append(
                    {
                        "repository_id": target.repository_id,
                        "number": target.number,
                        "created": False,
                        "status_code": 409,
                        "reason": "批量请求中存在重复目标",
                    }
                )
                continue
            seen.add(key)
            try:
                event, activity = await create_latest_manual_event(*key)
                inserted = await asyncio.to_thread(manager.store.enqueue_events, [event])
                if not inserted:
                    raise HTTPException(
                        status_code=409,
                        detail="手动事件未能写入，请重新操作",
                    )
            except HTTPException as exc:
                results.append(
                    {
                        "repository_id": target.repository_id,
                        "number": target.number,
                        "created": False,
                        "status_code": exc.status_code,
                        "reason": str(exc.detail),
                    }
                )
                continue

            created_count += 1
            results.append(
                {
                    "repository_id": target.repository_id,
                    "number": target.number,
                    "created": True,
                    "status_code": 200,
                    "event_id": event.id,
                    "event_type": event.type,
                    "source_activity_id": activity.id,
                    "source_occurred_at": (
                        activity.occurred_at.isoformat()
                        if activity.occurred_at is not None
                        else None
                    ),
                    "reason": f"已手动发送 {event.type}",
                }
            )

        if created_count:
            runtime.dispatch_events_now()
        failed_count = len(results) - created_count
        return {
            "requested": len(request.targets),
            "created": created_count,
            "failed": failed_count,
            "results": results,
            "reason": (
                f"批量手动触发完成：成功 {created_count} 项，失败 {failed_count} 项"
            ),
        }

    static_directory = Path(__file__).parent / "web" / "dist"
    index_file = static_directory / "index.html"
    if static_directory.is_dir():
        assets = static_directory / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def frontend(path: str):
            static_root = static_directory.resolve()
            requested = (static_root / path).resolve()
            if path and requested.is_file() and requested.is_relative_to(static_root):
                return FileResponse(requested)
            return FileResponse(index_file)
    else:
        @app.get("/", include_in_schema=False)
        async def frontend_missing():
            return JSONResponse(
                {"message": "管理 UI 尚未构建，请在 ui 目录运行 npm run build"}
            )

    return app
