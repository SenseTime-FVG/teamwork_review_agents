"""验证 Prompt 语言选择、模板一致性及环境暴露边界。"""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from teamwork_review_agents.config import load_config
from teamwork_review_agents.environment import (
    PromptRenderError,
    render_prompt,
    resolve_environment,
)
from teamwork_review_agents.executor import AgentExecutionError, AgentExecutor
from teamwork_review_agents.state import StateStore
from teamwork_review_agents.webapp import create_app


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPT_LANGUAGE_VARIABLES = (
    ("general-review.md", "GENERAL_REVIEWER_LANGUAGE"),
    ("依赖review.md", "DEPENDENCY_REVIEWER_LANGUAGE"),
    ("增量文档更新.md", "INCREMENTAL_DOC_UPDATER_LANGUAGE"),
    (
        "依赖review&增量文档更新 入口.md",
        "DEPENDENCY_AND_INCREMENTAL_DOC_UPDATE_RUNNER_LANGUAGE",
    ),
)
LOCAL_PROMPT_LANGUAGE_VARIABLES = (
    ("依赖review 入口.md", "DEPENDENCY_REVIEW_RUNNER_LANGUAGE"),
    ("增量文档更新入口.md", "INCREMENTAL_DOC_UPDATE_RUNNER_LANGUAGE"),
)
PROMPT_CASES = list(PROMPT_LANGUAGE_VARIABLES) + [
    pytest.param(
        filename,
        variable,
        marks=pytest.mark.skipif(
            not (PROJECT_ROOT / "prompts" / filename).is_file(),
            reason="本地独立入口不随项目分发",
        ),
    )
    for filename, variable in LOCAL_PROMPT_LANGUAGE_VARIABLES
]
LANGUAGE_RULE = "- 始终使用该语言进行回复、撰写报告或发表评论。"
LANGUAGE_TEMPLATE = "{{ AGENT_LANGUAGE | prompt_language(LANGUAGE) }}"


@pytest.mark.parametrize(("filename", "variable"), PROMPT_CASES)
@pytest.mark.parametrize(
    ("agent_language", "global_language", "expected"),
    [
        (None, None, "中文"),
        ("", "", "中文"),
        (" \t\n", "\n \t", "中文"),
        (None, "英文", "英文"),
        ("", "en", "英文"),
        (" \t\n", " English ", "英文"),
        ("中文", "英文", "中文"),
        (" EN ", "中文", "英文"),
        ("英文", None, "英文"),
        ("zh", "无效但不会读取的全局值", "中文"),
    ],
)
def test_each_prompt_selects_its_own_language(
    filename, variable, agent_language, global_language, expected,
) -> None:
    """每个模板都按专用变量、全局变量、中文默认值的顺序选择语言。"""

    template = (PROJECT_ROOT / "prompts" / filename).read_text(encoding="utf-8")
    values = {}
    if agent_language is not None:
        values[variable] = agent_language
    if global_language is not None:
        values["LANGUAGE"] = global_language
    rendered = render_prompt(template, values)

    assert rendered.startswith(f"# 使用语言\n\n{expected}\n\n{LANGUAGE_RULE}\n\n")
    assert rendered.count("# 使用语言\n") == 1
    assert "使用中文" not in rendered
    assert variable not in rendered
    assert "prompt_language" not in rendered
    # 语言章节之外只进行既有模板渲染，不在执行层重写其他规则。
    original_body = template.split(f"{LANGUAGE_RULE}\n\n", 1)[1]
    assert rendered.split(f"{LANGUAGE_RULE}\n\n", 1)[1] == render_prompt(
        original_body, values,
    )


@pytest.mark.parametrize(("filename", "variable"), PROMPT_CASES)
def test_prompt_ignores_other_agents_language(filename, variable) -> None:
    """其他 Agent 的专用语言不能成为当前 Agent 的默认语言。"""

    values = {
        name: "中文"
        for _, name in (*PROMPT_LANGUAGE_VARIABLES, *LOCAL_PROMPT_LANGUAGE_VARIABLES)
        if name != variable
    }
    values["LANGUAGE"] = "英文"
    template = (PROJECT_ROOT / "prompts" / filename).read_text(encoding="utf-8")

    assert render_prompt(template, values).startswith("# 使用语言\n\n英文\n")


@pytest.mark.parametrize("key", ["AGENT_LANGUAGE", "LANGUAGE"])
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("中文", "中文"), ("英文", "英文"),
        ("zh", "中文"), ("en", "英文"),
        (" ZH-cn ", "中文"), (" EN-us ", "英文"),
        (" CHINESE\n", "中文"), ("\tEnglish ", "英文"),
    ],
)
def test_language_aliases_are_normalized(key, value, expected) -> None:
    """两级变量使用相同的语言别名及首尾空白处理。"""

    assert render_prompt(LANGUAGE_TEMPLATE, {key: value}) == expected


@pytest.mark.parametrize("key", ["AGENT_LANGUAGE", "LANGUAGE"])
@pytest.mark.parametrize("value", ["invalid-sensitive-value", "{{ 7 * 7 }}", 1, False])
def test_invalid_nonempty_language_is_rejected_without_echo(key, value) -> None:
    """非法非空语言不能作为指令注入模板，也不应回显到错误中。"""

    with pytest.raises(PromptRenderError, match="Prompt 语言配置无效") as error:
        render_prompt(LANGUAGE_TEMPLATE, {key: value})
    assert str(value) not in str(error.value)


def test_null_values_fall_back_like_missing_values() -> None:
    """显式空值与未配置一致，不能渲染出 None。"""

    assert render_prompt(LANGUAGE_TEMPLATE, {"AGENT_LANGUAGE": None, "LANGUAGE": "en"}) == "英文"
    assert render_prompt(LANGUAGE_TEMPLATE, {"AGENT_LANGUAGE": None, "LANGUAGE": None}) == "中文"


def test_protocol_and_existing_document_language_rules_are_preserved() -> None:
    """选择英文不应翻译固定状态或取消已有文档的语言保留要求。"""

    dependency = render_prompt(
        (PROJECT_ROOT / "prompts/依赖review.md").read_text(encoding="utf-8"),
        {"LANGUAGE": "en"},
    )
    docs = render_prompt(
        (PROJECT_ROOT / "prompts/增量文档更新.md").read_text(encoding="utf-8"),
        {"LANGUAGE": "en"},
    )
    for status in ("UPDATED_AND_PUSHED", "FAILED", "BLOCKED"):
        assert status in dependency
        assert status in docs
    assert "NO_DEPENDENCY_UPDATE" in dependency
    assert "NO_DOCUMENT_UPDATE" in docs
    assert "保持原有语言、受众、语气、结构" in docs


@pytest.fixture
def language_config_path(tmp_path):
    """创建仅在临时目录使用的主子 Agent 语言配置。"""

    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "database": {"path": str(tmp_path / "state.db")},
                "environment": {"global": {"LANGUAGE": {
                    "value": "英文", "expose_to_prompt": True, "expose_to_process": False,
                }}},
                "providers": {"github": {
                    "kind": "github", "base_url": "https://api.github.com",
                    "token_env": "TEST_TOKEN",
                }},
                "repositories": [{
                    "id": "sample", "provider": "github", "project": "owner/sample",
                    "workspace": str(tmp_path / "workspace"),
                }],
                "agents": {
                    "parent": {
                        "prompt_file": str(PROJECT_ROOT / "prompts/general-review.md"),
                        "environment": {"GENERAL_REVIEWER_LANGUAGE": {
                            "value": "中文", "expose_to_prompt": True, "expose_to_process": False,
                        }},
                    },
                    "child": {"prompt_file": str(PROJECT_ROOT / "prompts/依赖review.md")},
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("agent_exposed", "global_exposed", "expected"),
    [(True, True, "中文"), (False, True, "英文"), (False, False, "中文")],
)
def test_environment_language_respects_prompt_exposure(
    language_config_path, agent_exposed, global_exposed, expected,
) -> None:
    """语言只读取 Prompt 上下文，不依赖进程暴露，也不绕过暴露开关。"""

    config = load_config(language_config_path)
    agent = config.agents["parent"]
    agent.environment["GENERAL_REVIEWER_LANGUAGE"].expose_to_prompt = agent_exposed
    config.environment.global_variables["LANGUAGE"].expose_to_prompt = global_exposed
    resolved = resolve_environment(config, config.repositories[0], agent, None, "test-run")

    rendered = render_prompt(agent.prompt_file.read_text(encoding="utf-8"), resolved.prompt_values)
    assert rendered.startswith(f"# 使用语言\n\n{expected}\n")
    assert "GENERAL_REVIEWER_LANGUAGE" not in resolved.process_values
    assert "LANGUAGE" not in resolved.process_values


def test_language_only_reads_explicit_host_environment(language_config_path, monkeypatch) -> None:
    """宿主语言必须通过显式来源引用才能影响 Prompt。"""

    monkeypatch.setenv("LANGUAGE", "en")
    assert render_prompt(LANGUAGE_TEMPLATE, {}) == "中文"
    config = load_config(language_config_path)
    definition = config.environment.global_variables["LANGUAGE"]
    definition.value = None
    definition.from_system = "LANGUAGE"
    agent = config.agents["child"]
    resolved = resolve_environment(config, config.repositories[0], agent, None, "child-run")
    assert render_prompt(LANGUAGE_TEMPLATE, resolved.prompt_values) == "英文"


def test_agent_execution_and_preview_use_the_same_language(language_config_path) -> None:
    """执行器与预览使用同一语言解析，父子 Agent 各自选择语言。"""

    config = load_config(language_config_path)
    repository = config.repositories[0]
    executor = AgentExecutor(config, StateStore(config.database.path))
    app = create_app(language_config_path, start_scheduler=False)
    with TestClient(app) as client:
        for name, language in (("parent", "中文"), ("child", "英文")):
            agent = config.agents[name]
            resolved = resolve_environment(config, repository, agent, None, name)
            prompt = executor.build_prompt(
                agent_name=name, event=None, repository=repository, task="测试任务",
                extra_context=None, prompt_values=resolved.prompt_values,
                change_ref="test-ref", actions=[],
            )
            preview = client.post("/api/prompts/preview", json={
                "template": agent.prompt_file.read_text(encoding="utf-8"),
                "variables": resolved.prompt_values,
            })
            assert preview.status_code == 200
            assert preview.json()["rendered"].startswith(f"# 使用语言\n\n{language}\n")
            assert prompt.startswith(preview.json()["rendered"].strip())

        invalid = client.post("/api/prompts/preview", json={
            "template": LANGUAGE_TEMPLATE,
            "variables": {"LANGUAGE": "invalid-sensitive-value"},
        })
        assert invalid.status_code == 422
        assert "Prompt 语言配置无效" in invalid.json()["detail"]
        assert "invalid-sensitive-value" not in invalid.text

    with pytest.raises(AgentExecutionError, match="Prompt 语言配置无效"):
        executor.build_prompt(
            agent_name="child", event=None, repository=repository, task="测试任务",
            extra_context=None, prompt_values={"LANGUAGE": "invalid-sensitive-value"},
            change_ref="test-ref", actions=[],
        )
