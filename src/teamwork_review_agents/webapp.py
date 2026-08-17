"""FastAPI 管理 API、SSE 日志和 React 静态资源入口。"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config_manager import ConfigManager
from .codex_settings import inspect_runtime_options
from .environment import render_prompt
from .events import FIELD_EVENTS, detect_events
from .prompt_files import MAX_PROMPT_FILE_BYTES, import_prompt_file, list_prompt_files
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

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if start_scheduler:
            await runtime.start()
        try:
            yield
        finally:
            if start_scheduler:
                await runtime.stop()

    app = FastAPI(
        title="Teamwork Review Agents",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.config_manager = manager
    app.state.runtime = runtime

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
        return {"status": "ok", "version": app.version}

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
                *sorted(set(FIELD_EVENTS.values())),
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

    @app.get("/api/codex/runtime-options")
    async def codex_runtime_options() -> dict[str, Any]:
        """返回本机 Codex 模型目录和当前可验证的继承模型来源。"""

        return await asyncio.to_thread(
            inspect_runtime_options,
            manager.config.runtime.codex,
            manager.config.runtime.codex_binary,
        )

    @app.post("/api/prompts/preview")
    async def preview_prompt(body: PromptPreviewRequest) -> dict[str, str]:
        """按运行时相同规则预览模板，未定义变量渲染为空字符串。"""

        return {"rendered": render_prompt(body.template, body.variables)}

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
        limit: int = Query(default=50, ge=1, le=500),
        status: str | None = None,
    ):
        return await asyncio.to_thread(
            manager.store.list_events,
            limit,
            status=status,
        )

    @app.get("/api/change-requests")
    async def change_requests(
        limit: int = Query(default=100, ge=1, le=500),
        repository_id: str | None = None,
    ):
        """返回扫描器已经建立基线的 MR/PR 最新快照。"""

        return await asyncio.to_thread(
            manager.store.list_snapshots,
            limit,
            repository_id=repository_id,
        )

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
            runtime.scan_now()
        return {
            "created": bool(inserted),
            "event_id": event.id,
            "reason": "首次发现事件已补发" if inserted else "首次发现事件已经存在",
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
