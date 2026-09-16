"""模型签名语言的配置兼容、持久化和空正文边界测试。"""

import pytest
from fastapi.testclient import TestClient

from teamwork_review_agents.config import AgentConfig, load_config
from teamwork_review_agents.managed_comments import ManagedCommentService
from teamwork_review_agents.state import StateStore
from teamwork_review_agents.webapp import create_app


@pytest.mark.parametrize("language", ["zh", "en", "bilingual"])
def test_signature_language_round_trips(language) -> None:
    """三种签名语言可解析并序列化，不依赖是否启用签名。"""

    agent = AgentConfig.model_validate({
        "prompt": "测试",
        "managed_comment_model_signature_language": language,
    })
    assert agent.managed_comment_model_signature_language == language
    assert agent.model_dump()["managed_comment_model_signature_language"] == language
    assert agent.managed_comment_model_signature is False


@pytest.mark.parametrize("language", ["fr", "", None, 1])
def test_signature_language_rejects_values_outside_ui_options(language) -> None:
    """确定性签名文案仅有三种选项，与支持任意文本的 Prompt 语言区分。"""

    with pytest.raises(ValueError, match="managed_comment_model_signature_language"):
        AgentConfig.model_validate({
            "prompt": "测试",
            "managed_comment_model_signature_language": language,
        })


def test_signature_language_api_save_and_readback(configured_app_factory) -> None:
    """语言选项保存后可回读，关闭签名和托管评论不能清空已保存的选项。"""

    config = configured_app_factory()
    app = create_app(config.config_path, start_scheduler=False)
    with TestClient(app) as client:
        current = client.get("/api/config").json()
        agent = dict(current["document"]["agents"]["security-reviewer"])
        agent.update({
            "managed_comment": True,
            "managed_comment_model_signature": True,
            "managed_comment_slot": "signature-language-test",
            "write_scopes": ["change_request"],
        })
        for language in ("en", "bilingual", "zh"):
            agent["managed_comment_model_signature_language"] = language
            response = client.put("/api/config/agents/security-reviewer", json={
                "revision": current["revision"],
                "name": "security-reviewer",
                "agent": agent,
            })
            assert response.status_code == 200
            current = response.json()
            assert current["document"]["agents"]["security-reviewer"][
                "managed_comment_model_signature_language"
            ] == language
            saved = load_config(config.config_path).agents["security-reviewer"]
            assert saved.managed_comment_model_signature_language == language

        agent.update({
            "managed_comment": False,
            "managed_comment_model_signature": False,
            "managed_comment_model_signature_language": "bilingual",
        })
        response = client.put("/api/config/agents/security-reviewer", json={
            "revision": current["revision"],
            "name": "security-reviewer",
            "agent": agent,
        })
        assert response.status_code == 200
        reread = client.get("/api/config").json()["document"]["agents"]["security-reviewer"]
        assert reread["managed_comment_model_signature_language"] == "bilingual"
        assert reread["managed_comment_model_signature"] is False
        assert reread["managed_comment"] is False


@pytest.mark.parametrize("language", ["zh", "en", "bilingual"])
@pytest.mark.parametrize("body", ["", " \n\t"])
async def test_signature_does_not_turn_blank_body_into_comment(
    configured_app_factory, monkeypatch, language, body,
) -> None:
    """空正文不因翻译签名而变成可发布评论，且不读取运行快照。"""

    config = configured_app_factory()
    store = StateStore(config.database.path)

    def unexpected_get_run(_run_id):
        """空正文无需访问运行记录。"""
        pytest.fail("空正文不应读取模型快照")

    monkeypatch.setattr(store, "get_run", unexpected_get_run)
    service = ManagedCommentService(config, store)
    assert await service._append_model_signature("unused", body, language=language) == body
