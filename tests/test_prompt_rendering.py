"""Prompt 的 Jinja 条件渲染与旧变量语法兼容测试。"""

import shlex
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from teamwork_review_agents.environment import PromptRenderError, render_prompt
from teamwork_review_agents.webapp import create_app


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_legacy_and_native_variables_render_together() -> None:
    """旧变量语法与 Jinja 原生变量应得到相同的兼容结果。"""

    rendered = render_prompt(
        "旧=${{OLD}} 新={{ NEW }} 缺失=${{MISSING}}/{{ ALSO_MISSING }}",
        {"OLD": "旧值", "NEW": "新值"},
    )

    assert rendered == "旧=旧值 新=新值 缺失=/"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", "开启"),
        (" TRUE ", "开启"),
        ("false", "关闭"),
        ("1", "关闭"),
        ("", "关闭"),
        (None, "关闭"),
    ],
)
def test_as_bool_only_accepts_explicit_true(
    value: str | None,
    expected: str,
) -> None:
    """自动合并开关只有显式 true 才能进入真分支。"""

    values = {} if value is None else {"FLAG": value}
    rendered = render_prompt(
        "{% if FLAG | as_bool %}开启{% else %}关闭{% endif %}",
        values,
    )

    assert rendered == expected


def test_nested_conditions_and_variable_content_are_safe() -> None:
    """嵌套条件可用，变量内的 Jinja 标记不得被二次执行。"""

    template = (
        "{% if OUTER | as_bool %}"
        "{% if INNER | as_bool %}${{VALUE}}{% else %}内层关闭{% endif %}"
        "{% else %}外层关闭{% endif %}"
    )
    payload = "{% if true %}不应执行{% endif %}"

    assert render_prompt(
        template,
        {"OUTER": "true", "INNER": "true", "VALUE": payload},
    ) == payload
    assert render_prompt(
        template,
        {"OUTER": "true", "INNER": "false", "VALUE": payload},
    ) == "内层关闭"


def test_invalid_jinja_is_reported_as_prompt_error() -> None:
    """模板语法错误应转换成稳定的 Prompt 配置错误。"""

    with pytest.raises(PromptRenderError, match="Prompt 模板渲染失败"):
        render_prompt("{% if FLAG %}缺少结束标签", {"FLAG": "true"})


def test_general_review_renders_exclusive_final_policy() -> None:
    """通用审核 Prompt 每次只能保留一种最终处理策略。"""

    template = (PROJECT_ROOT / "prompts" / "general-review.md").read_text(
        encoding="utf-8"
    )
    automatic = render_prompt(template, {"REVIEW_AUTO_MERGE": "true"})
    review_only = render_prompt(template, {"REVIEW_AUTO_MERGE": "false"})
    missing = render_prompt(template, {})

    assert "本轮操作模式：审核并自动合并" in automatic
    assert "## 合并方式" in automatic
    assert "## 仅审核结束" not in automatic
    assert "本轮操作模式：仅审核，不自动合并" not in automatic

    for rendered in (review_only, missing):
        assert "本轮操作模式：仅审核，不自动合并" in rendered
        assert "## 仅审核结束" in rendered
        assert "## 合并方式" not in rendered
        assert "本轮操作模式：审核并自动合并" not in rendered


@pytest.mark.parametrize("auto_merge", ["true", "false", None])
def test_general_review_preserves_description_without_reading_comments(
    auto_merge: str | None,
) -> None:
    """所有审核模式保留描述，禁止读取评论，并继续检查门禁和发布本轮结果。"""

    template = (PROJECT_ROOT / "prompts/general-review.md").read_text(encoding="utf-8")
    values = {} if auto_merge is None else {"REVIEW_AUTO_MERGE": auto_merge}
    rendered = render_prompt(template, values)
    boundary = rendered.split("## 审核材料边界", 1)[1].split("# 三、", 1)[0]

    assert "GitHub `body` / GitLab `description`" in rendered
    assert "正常读取 MR/PR 标题和描述正文" in boundary
    assert "描述也不属于评论列表" in boundary
    assert "描述为空时如实记录，不读取第一条评论补全描述" in boundary
    assert "不读取任何人工或机器人顶层评论" in boundary
    assert "历史自动审核结果、行内评审评论、评审意见正文及讨论线程" in boundary
    assert "不保留按需读取人工讨论的例外" in boundary
    assert "不得请求 `comments`、`notes`、讨论正文或完整评审正文" in boundary
    assert "不使用 `--comments`" in boundary
    assert "不将其作为审核证据、本轮执行事实或继续获取讨论的依据" in boundary
    assert "已有讨论" not in rendered
    assert "不因为历史评论声称已审核过就跳过当前任务" not in rendered

    # 示例必须选择描述字段，不能通过完整 PR 输出重新带入评论或评审正文。
    command = next(line.strip() for line in boundary.splitlines() if line.strip().startswith("gh pr view "))
    arguments = shlex.split(command)
    fields = set(arguments[arguments.index("--json") + 1].split(","))
    assert {"title", "body", "commits", "reviewDecision", "statusCheckRollup"} <= fields
    assert not fields.intersection({"comments", "reviews", "latestReviews", "reviewThreads", "timelineItems"})
    assert "--comments" not in arguments

    # 不读意见正文不能放宽审批、讨论解决状态或其他平台门禁。
    assert "审批是否满足、讨论是否已解决、CI、分支保护和可合并状态等平台门禁仍须检查" in boundary
    assert "无法可靠确认门禁时，按原有门禁规则处理，不得默认放行" in boundary
    assert "项目要求的人工审批、讨论解决、分支保护和其他合并门禁均已满足" in rendered
    assert "最终顶层评论必须调用该工具进行发布或更新" in rendered
    assert "不得使用 `gh`、`glab` 或平台 API 另行发布顶层总结评论" in rendered


def test_builtin_prompts_render_configured_environment_values_directly() -> None:
    """内置 Prompt 应直接显示已暴露配置，不要求模型通过 Shell 读取。"""

    general_template = (PROJECT_ROOT / "prompts" / "general-review.md").read_text(
        encoding="utf-8"
    )
    general = render_prompt(
        general_template,
        {
            "REVIEW_AUTO_MERGE": "false",
            "REVIEW_SKILLS": "security-review, style-review",
            "REVIEW_DESIGN_DOC_DIR": "docs/design",
            "REVIEW_CHANGE_HISTORY_DIR": "docs/changes",
        },
    )

    assert "security-review, style-review" in general
    assert "docs/design" in general
    assert "docs/changes" in general
    assert "Teamwork" not in general
    assert "REVIEW_SKILLS" not in general
    assert "REVIEW_DESIGN_DOC_DIR" not in general
    assert "REVIEW_CHANGE_HISTORY_DIR" not in general

    runner_template = (
        PROJECT_ROOT / "prompts/依赖review&增量文档更新 入口.md"
    ).read_text(
        encoding="utf-8"
    )
    runner = render_prompt(
        runner_template,
        {
            "DEPENDENCY_AUTO_UPDATE_AGENT_NAME": "dependency-reviewer",
            "INCREMENTAL_DOC_UPDATE_AGENT_NAME": "incremental-doc-updater",
        },
    )
    assert "<依赖更新 Agent 名称>\ndependency-reviewer\n</依赖更新 Agent 名称>" in runner
    assert "<文档更新 Agent 名称>\nincremental-doc-updater\n</文档更新 Agent 名称>" in runner
    assert "DEPENDENCY_AUTO_UPDATE_AGENT_NAME" not in runner
    assert "INCREMENTAL_DOC_UPDATE_AGENT_NAME" not in runner

    updater_template = (PROJECT_ROOT / "prompts/增量文档更新.md").read_text(
        encoding="utf-8"
    )
    updater = render_prompt(
        updater_template,
        {
            "DOC_UPDATE_REPOSITORY_ROOT": "/workspace/project",
            "DOC_UPDATE_EXCLUDE_DIRECTORIES": ".cache, generated",
            "DOC_UPDATE_INDEX_PATH": "docs/index.md",
        },
    )
    assert "/workspace/project" in updater
    assert ".cache, generated" in updater
    assert "docs/index.md" in updater
    assert "${DOC_UPDATE_REPOSITORY_ROOT}" not in updater
    assert "${DOC_UPDATE_EXCLUDE_DIRECTORIES}" not in updater
    assert "${DOC_UPDATE_INDEX_PATH}" not in updater
    assert "DOC_UPDATE_REPOSITORY_ROOT" not in updater
    assert "DOC_UPDATE_EXCLUDE_DIRECTORIES" not in updater
    assert "DOC_UPDATE_INDEX_PATH" not in updater


def test_builtin_prompt_environment_values_default_to_empty() -> None:
    """未配置的环境变量应渲染为空，并交由 Prompt 的默认规则处理。"""

    updater_template = (PROJECT_ROOT / "prompts/增量文档更新.md").read_text(
        encoding="utf-8"
    )
    updater = render_prompt(updater_template, {})

    assert "<仓库根目录>\n\n</仓库根目录>" in updater
    assert "<排除目录>\n\n</排除目录>" in updater
    assert "<文档索引路径>\n\n</文档索引路径>" in updater


def test_prompt_preview_uses_same_jinja_renderer(tmp_path) -> None:
    """管理 API 预览必须与 Agent 执行复用同一渲染规则。"""

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
        response = client.post(
            "/api/prompts/preview",
            json={
                "template": (
                    "${{NAME}}："
                    "{% if REVIEW_AUTO_MERGE | as_bool %}合并{% else %}仅审核{% endif %}"
                ),
                "variables": {"NAME": "review", "REVIEW_AUTO_MERGE": "false"},
            },
        )
        invalid = client.post(
            "/api/prompts/preview",
            json={"template": "{% if FLAG %}", "variables": {"FLAG": "true"}},
        )

    assert response.status_code == 200
    assert response.json()["rendered"] == "review：仅审核"
    assert invalid.status_code == 422
    assert "Prompt 模板渲染失败" in invalid.json()["detail"]
