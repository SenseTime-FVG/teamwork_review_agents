"""FastAPI 管理 API、SSE 日志和 React 静态资源入口。"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config_manager import ConfigManager
from .environment import render_prompt
from .events import FIELD_EVENTS
from .runtime import BackgroundRuntime


class ConfigDocumentRequest(BaseModel):
    """UI 提交的完整配置文档。"""

    document: dict[str, Any]


class PromptPreviewRequest(BaseModel):
    """Prompt 模板的纯文本预览请求。"""

    template: str
    variables: dict[str, str] = Field(default_factory=dict)


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

    @app.post("/api/prompts/preview")
    async def preview_prompt(body: PromptPreviewRequest) -> dict[str, str]:
        """按运行时相同规则预览模板，未定义变量渲染为空字符串。"""

        return {"rendered": render_prompt(body.template, body.variables)}

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
