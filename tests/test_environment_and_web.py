"""分层环境、配置管理和 Web API 测试。"""

from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from teamwork_review_agents.config import load_config
from teamwork_review_agents.config_manager import ConfigManager
from teamwork_review_agents.environment import (
    MASK,
    SecretRedactor,
    render_prompt,
    resolve_environment,
    resolve_provider_token,
)
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.models import AgentResult
from teamwork_review_agents.webapp import create_app


def write_config(tmp_path):
    """创建包含两级 Secret 和内联 Prompt 的最小配置。"""

    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    document = {
        "database": {"path": str(tmp_path / "state.db")},
        "scanner": {"interval_seconds": 60},
        "environment": {
            "global": {
                "LEVEL": "global",
                "GITHUB_TEST_TOKEN": {
                    "value": "provider-secret",
                    "secret": False,
                    "expose_to_prompt": True,
                    "expose_to_process": True,
                },
                "GLOBAL_SECRET": {
                    "from_system": "TEST_GLOBAL_SECRET",
                    "secret": True,
                },
            }
        },
        "providers": {
            "provider-main": {
                "kind": "github",
                "base_url": "https://api.github.com",
                "token_env": "GITHUB_TEST_TOKEN",
            }
        },
        "repositories": [
            {
                "id": "first",
                "provider": "provider-main",
                "project": "owner/first",
                "workspace": str(first_workspace),
                "environment": {
                    "LEVEL": "repository",
                    "REPOSITORY_SECRET": {
                        "value": "first-secret",
                        "secret": True,
                    },
                },
            },
            {
                "id": "second",
                "provider": "provider-main",
                "project": "owner/second",
                "workspace": str(second_workspace),
                "environment": {
                    "REPOSITORY_SECRET": {
                        "value": "second-secret",
                        "secret": True,
                    }
                },
            },
        ],
        "agents": {
            "reviewer": {
                "prompt": "级别=${{LEVEL}} Secret=${{GLOBAL_SECRET}} Missing=${{MISSING}}",
                "sandbox": "read-only",
                "environment": {
                    "LEVEL": "agent",
                    "MR_NUMBER": "不能覆盖运行变量",
                },
            }
        },
        "rules": [
            {
                "name": "review",
                "events": ["change_request.discovered"],
                "agents": ["reviewer"],
            }
        ],
    }
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_environment_precedence_template_and_redaction(
    tmp_path,
    monkeypatch,
    snapshot_factory,
) -> None:
    config_path = write_config(tmp_path)
    monkeypatch.setenv("TEST_GLOBAL_SECRET", "system-secret")
    config = load_config(config_path)
    repository = config.repository_map()["first"]
    snapshot = snapshot_factory(
        provider=repository.provider,
        repository_id=repository.id,
    )
    event = detect_events(None, snapshot, emit_initial=True)[0]
    resolved = resolve_environment(
        config,
        repository,
        config.agents["reviewer"],
        event,
        "run-test",
    )

    assert resolved.all_values["LEVEL"] == "agent"
    assert resolved.all_values["MR_NUMBER"] == "7"
    assert resolved.prompt_values["GLOBAL_SECRET"] == ""
    assert resolved.process_values["GLOBAL_SECRET"] == "system-secret"
    assert resolved.audit_values["GLOBAL_SECRET"] == MASK
    assert resolved.prompt_values["GITHUB_TEST_TOKEN"] == ""
    assert "GITHUB_TEST_TOKEN" not in resolved.process_values
    assert resolved.audit_values["GITHUB_TEST_TOKEN"] == MASK
    assert render_prompt("${{LEVEL}}/${{MISSING}}", resolved.prompt_values) == "agent/"
    assert SecretRedactor(resolved.secret_values).text(
        "system-secret first-secret"
    ) == f"{MASK} {MASK}"

    delegated = resolve_environment(
        config,
        repository,
        config.agents["reviewer"],
        event,
        "run-child",
        include_change_request=False,
    )
    assert delegated.all_values["REPOSITORY_ID"] == repository.id
    assert delegated.all_values["RUN_ID"] == "run-child"
    assert "MR_TITLE" not in delegated.all_values
    assert "EVENT_TYPE" not in delegated.process_values


def test_config_manager_masks_and_merges_reordered_repositories(tmp_path) -> None:
    config_path = write_config(tmp_path)
    manager = ConfigManager(config_path)
    document = manager.document()
    provider_token = document["environment"]["global"]["GITHUB_TEST_TOKEN"]
    assert provider_token["value"] == MASK
    assert provider_token["secret"] is True
    assert provider_token["expose_to_prompt"] is False
    assert provider_token["expose_to_process"] is False
    assert document["repositories"][0]["environment"]["REPOSITORY_SECRET"]["value"] == MASK
    assert document["repositories"][1]["environment"]["REPOSITORY_SECRET"]["value"] == MASK

    document["repositories"].reverse()
    manager.save(document)
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    secrets = {
        item["id"]: item["environment"]["REPOSITORY_SECRET"]["value"]
        for item in saved["repositories"]
    }
    assert secrets == {"first": "first-secret", "second": "second-secret"}
    saved_provider_token = saved["environment"]["global"]["GITHUB_TEST_TOKEN"]
    assert saved_provider_token["value"] == "provider-secret"
    assert saved_provider_token["secret"] is True
    assert saved_provider_token["expose_to_prompt"] is False
    assert saved_provider_token["expose_to_process"] is False
    versions = manager.store.list_config_versions()
    assert versions
    assert MASK in manager.store.get_config_version(versions[0]["revision"])["content"]
    assert "first-secret" not in manager.store.get_config_version(versions[0]["revision"])["content"]
    assert "provider-secret" not in manager.store.get_config_version(versions[0]["revision"])["content"]


def test_provider_token_uses_global_config_then_host_fallback(tmp_path, monkeypatch) -> None:
    """Provider Token 应按全局固定值、全局宿主机引用、直接宿主机依次解析。"""

    config_path = write_config(tmp_path)
    monkeypatch.setenv("GITHUB_TEST_TOKEN", "host-fallback-token")
    config = load_config(config_path)
    provider = config.providers["provider-main"]
    assert resolve_provider_token(config, provider) == "provider-secret"

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    document["environment"]["global"]["GITHUB_TEST_TOKEN"] = {
        "from_system": "TEAMWORK_GITHUB_TOKEN",
    }
    monkeypatch.setenv("TEAMWORK_GITHUB_TOKEN", "referenced-host-token")
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert resolve_provider_token(config, config.providers["provider-main"]) == "referenced-host-token"

    del document["environment"]["global"]["GITHUB_TEST_TOKEN"]
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert resolve_provider_token(config, config.providers["provider-main"]) == "host-fallback-token"


def test_web_api_config_preview_logs_and_static_ui(tmp_path, snapshot_factory) -> None:
    config_path = write_config(tmp_path)
    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        response = client.get("/api/config")
        assert response.status_code == 200
        document = response.json()["document"]
        assert document["repositories"][0]["environment"]["REPOSITORY_SECRET"]["value"] == MASK

        preview = client.post(
            "/api/prompts/preview",
            json={"template": "A=${{A}}, B=${{B}}", "variables": {"A": "1"}},
        )
        assert preview.json()["rendered"] == "A=1, B="

        assert client.get("/api/prompt-files").json() == []
        imported = client.post(
            "/api/prompt-files/import",
            files={"file": ("review prompt.md", "请审查当前变更。", "text/markdown")},
        )
        assert imported.status_code == 200
        assert imported.json()["path"] == "./prompts/review-prompt.md"
        assert client.get("/api/prompt-files").json()[0]["name"] == "review-prompt.md"

        duplicate = client.post(
            "/api/prompt-files/import",
            files={"file": ("review prompt.md", "请审查当前变更。", "text/markdown")},
        )
        assert duplicate.json()["path"] == "./prompts/review-prompt.md"

        renamed = client.post(
            "/api/prompt-files/import",
            files={"file": ("review prompt.md", "不同的内容", "text/markdown")},
        )
        assert renamed.json()["path"] == "./prompts/review-prompt-2.md"

        unsupported = client.post(
            "/api/prompt-files/import",
            files={"file": ("prompt.json", "{}", "application/json")},
        )
        assert unsupported.status_code == 422

        invalid = document | {
            "agents": {
                **document["agents"],
                "reviewer": {**document["agents"]["reviewer"], "prompt": ""},
            }
        }
        assert client.put("/api/config", json={"document": invalid}).status_code == 422

        store = app.state.config_manager.store
        snapshot = snapshot_factory(
            provider="provider-main",
            repository_id="first",
            number=12,
            title="等待首次事件的 PR",
        )
        store.save_snapshot_and_events(snapshot, [])
        change_requests = client.get("/api/change-requests").json()
        assert change_requests[0]["number"] == 12
        assert change_requests[0]["discovered_event_emitted"] is False
        assert client.get("/api/status").json()["stats"]["change_requests"] == {
            "total": 1,
            "opened": 1,
        }

        emitted = client.post(
            "/api/change-requests/first/12/emit-discovered"
        ).json()
        assert emitted["created"] is True
        repeated = client.post(
            "/api/change-requests/first/12/emit-discovered"
        ).json()
        assert repeated == {"created": False, "reason": "首次发现事件已经存在"}
        assert client.get("/api/change-requests").json()[0][
            "discovered_event_emitted"
        ] is True
        assert len(store.pending_events()) == 1
        event_record = client.get("/api/events").json()[0]
        assert event_record["status"] == "pending"
        assert event_record["trigger_count"] == 0
        assert event_record["agent_running_count"] == 0
        assert client.post(
            "/api/change-requests/first/999/emit-discovered"
        ).status_code == 404

        reservation = store.begin_agent_run(
            proposed_run_id="run-web",
            root_run_id=None,
            parent_run_id=None,
            idempotency_key="web-test",
            event_id=None,
            rule_name="review",
            agent_name="reviewer",
            resource_key="github:first:7",
            prompt="测试 Prompt",
            environment={"SECRET": MASK},
            config_revision="revision-test",
            max_attempts=1,
        )
        assert reservation is not None
        store.append_run_log(
            "run-web",
            stream="stdout",
            event_type="item.completed",
            payload={"message": "完成"},
        )
        store.finish_agent_run(
            AgentResult(
                run_id="run-web",
                root_run_id="run-web",
                agent_name="reviewer",
                status="completed",
                final_message="完成",
            )
        )
        assert client.get("/api/runs/run-web").json()["environment"] == {"SECRET": MASK}
        assert client.get("/api/runs/run-web/logs").json()[0]["event_type"] == "item.completed"
        stream = client.get("/api/runs/run-web/stream")
        assert "event: item.completed" in stream.text
        assert "event: end" in stream.text

        cancellable = store.begin_agent_run(
            proposed_run_id="run-web-cancel",
            root_run_id=None,
            parent_run_id=None,
            idempotency_key="web-cancel-test",
            event_id=None,
            rule_name="review",
            agent_name="reviewer",
            resource_key="github:first:12",
            prompt="等待执行",
            max_attempts=1,
        )
        assert cancellable is not None
        cancelled = client.post("/api/runs/run-web-cancel/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["accepted"] is True
        assert client.get("/api/runs/run-web-cancel").json()["status"] == "cancelled"
        cancel_logs = client.get("/api/runs/run-web-cancel/logs").json()
        assert cancel_logs[-1]["event_type"] == "run.cancel_requested"
        assert client.post("/api/runs/unknown/cancel").status_code == 404

        static = client.get("/")
        assert static.status_code == 200
        assert "Teamwork Review Agents" in static.text


def test_codex_runtime_options_report_catalog_and_user_model(
    tmp_path,
    monkeypatch,
) -> None:
    """运行时接口应返回本机模型目录和可验证的用户模型来源。"""

    config_path = write_config(tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "gpt-user"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "printf '%s' '{\"models\":[{\"slug\":\"gpt-test\",\"display_name\":\"GPT Test\",\"default_reasoning_level\":\"medium\",\"supported_reasoning_levels\":[{\"effort\":\"low\"},{\"effort\":\"medium\"}],\"additional_speed_tiers\":[\"fast\"]}]}'\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    document["runtime"] = {"codex_binary": str(fake_codex)}
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        result = client.get("/api/codex/runtime-options").json()

    assert result["models"][0]["slug"] == "gpt-test"
    assert result["models"][0]["supported_reasoning_levels"] == ["low", "medium"]
    assert result["models"][0]["supports_fast_mode"] is True
    assert result["user_model"] == "gpt-user"
    assert result["inherited_model"] == {
        "value": "gpt-user",
        "source": "user",
        "label": "继承 Codex 用户配置（gpt-user）",
    }
