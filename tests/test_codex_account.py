"""独立 Codex Home 账户读取与登录测试。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from teamwork_review_agents.codex_account import (
    CodexLoginManager,
    inspect_codex_account,
    read_codex_effective_config,
)
from teamwork_review_agents.webapp import create_app


def _write_fake_codex(path: Path) -> Path:
    """创建只实现测试所需 App Server 协议的 Codex 命令。"""

    path.write_text(
        """#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        continue
    if method == "initialize":
        result = {"serverInfo": {"name": "fake-codex"}}
    elif method == "account/read":
        result = {
            "account": {
                "type": "chatgpt",
                "email": "developer@example.com",
                "planType": "plus",
                "credentialSource": "file",
                "accessToken": "不得返回的凭据",
            },
            "requiresOpenaiAuth": True,
        }
    elif method == "account/rateLimits/read":
        result = {
            "rateLimits": {
                "limitId": "codex",
                "limitName": "Codex",
                "primary": {
                    "usedPercent": 25,
                    "windowDurationMins": 300,
                    "resetsAt": 1800000000,
                    "private": "不得返回",
                },
            },
            "unknown": "不得返回",
        }
    elif method == "account/usage/read":
        result = {
            "summary": {"lifetimeTokens": 1234, "private": "不得返回"},
            "unknown": "不得返回",
        }
    elif method == "account/login/start":
        result = {"loginId": "login-test", "authUrl": "https://example.com/login"}
    elif method == "account/login/cancel":
        result = {"cancelled": True}
    elif method == "config/read":
        result = {
            "config": {
                "model": "gpt-effective",
                "service_tier": "fast",
            }
        }
    else:
        result = {}
    print(json.dumps({"id": request_id, "result": result}), flush=True)
    if method == "account/login/start":
        print(json.dumps({
            "method": "account/login/completed",
            "params": {"loginId": "login-test", "success": True},
        }), flush=True)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


async def test_inspect_codex_account_returns_only_safe_fields(tmp_path) -> None:
    """账户接口应裁剪凭据和协议中的未知字段。"""

    fake_codex = _write_fake_codex(tmp_path / "fake-codex")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    result = await inspect_codex_account(str(fake_codex), codex_home)

    assert result["status"] == "signed_in"
    assert result["account"] == {
        "type": "chatgpt",
        "email": "developer@example.com",
        "planType": "plus",
        "credentialSource": "file",
    }
    assert result["rate_limits"] == {
        "rateLimits": {
            "limitId": "codex",
            "limitName": "Codex",
            "primary": {
                "usedPercent": 25,
                "windowDurationMins": 300,
                "resetsAt": 1800000000,
            },
        }
    }
    assert result["usage"] == {"summary": {"lifetimeTokens": 1234}}
    assert "accessToken" not in result["account"]


async def test_read_codex_effective_config_uses_app_server(tmp_path) -> None:
    """有效配置诊断应使用 App Server 完成配置分层。"""

    fake_codex = _write_fake_codex(tmp_path / "fake-codex")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    result = await read_codex_effective_config(str(fake_codex), codex_home)

    assert result == {"model": "gpt-effective", "service_tier": "fast"}


async def test_missing_independent_home_is_signed_out_without_creation(tmp_path) -> None:
    """账户只读查询不应擅自创建尚不存在的独立目录。"""

    codex_home = tmp_path / "missing-home"
    result = await inspect_codex_account("missing-codex", codex_home)

    assert result["managed"] is True
    assert result["status"] == "signed_out"
    assert not codex_home.exists()


async def test_login_manager_completes_browser_login_and_closes_process(tmp_path) -> None:
    """浏览器登录完成通知应更新会话并回收 App Server。"""

    fake_codex = _write_fake_codex(tmp_path / "fake-codex")
    codex_home = tmp_path / "new-codex-home"
    manager = CodexLoginManager()
    try:
        started = await manager.start(str(fake_codex), codex_home)
        assert started["status"] == "pending"
        assert started["auth_url"] == "https://example.com/login"
        for _ in range(50):
            session = manager.get(started["session_id"])
            if session and session["status"] != "pending":
                break
            await asyncio.sleep(0.01)
        assert session is not None
        assert session["status"] == "completed"
        assert codex_home.is_dir()
    finally:
        await manager.close()


def test_codex_account_and_login_web_api(tmp_path) -> None:
    """管理 API 应读取独立账户并提供可轮询的登录会话。"""

    fake_codex = _write_fake_codex(tmp_path / "fake-codex")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "database": {"path": str(tmp_path / "state.db")},
                "runtime": {
                    "codex_binary": str(fake_codex),
                    "codex_home": str(codex_home),
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        account = client.get("/api/codex/account")
        assert account.status_code == 200
        assert account.json()["account"]["email"] == "developer@example.com"

        started = client.post("/api/codex/login")
        assert started.status_code == 200
        session = started.json()
        for _ in range(50):
            current = client.get(f"/api/codex/login/{session['session_id']}")
            assert current.status_code == 200
            session = current.json()
            if session["status"] != "pending":
                break
            time.sleep(0.01)
        assert session["status"] == "completed"


def test_codex_login_web_api_requires_saved_independent_home(tmp_path) -> None:
    """未配置独立 Codex Home 时应保持原行为且不开放登录入口。"""

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {"database": {"path": str(tmp_path / "state.db")}},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        assert client.get("/api/codex/account").json()["status"] == "inherited"
        response = client.post("/api/codex/login")
        assert response.status_code == 409
