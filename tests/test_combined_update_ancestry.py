"""使用真实 Git 提交拓扑验证组合更新入口的祖先校验模板。"""

import os
from pathlib import Path
import shlex
import subprocess

import pytest

from teamwork_review_agents.environment import render_prompt


PROMPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "prompts"
    / "依赖review&增量文档更新 入口.md"
)


def ancestry_commands() -> list[list[str]]:
    """从实际渲染的入口提取命令，避免测试自造的模板与生产内容脱节。"""

    rendered = render_prompt(
        PROMPT_PATH.read_text(encoding="utf-8"),
        {
            "DEPENDENCY_AUTO_UPDATE_AGENT_NAME": "dependency-reviewer",
            "INCREMENTAL_DOC_UPDATE_AGENT_NAME": "incremental-doc-updater",
        },
    )
    return [
        shlex.split(line)
        for line in rendered.splitlines()
        if line.startswith("git merge-base --is-ancestor ")
    ]


def run_git(
    repository: Path, *arguments: str, input_text: str = ""
) -> subprocess.CompletedProcess[str]:
    """仅在测试临时仓库执行 Git，并保留原始退出码和错误输出。"""

    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *arguments],
        cwd=repository,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Ancestry Test",
            "GIT_AUTHOR_EMAIL": "ancestry@example.test",
            "GIT_COMMITTER_NAME": "Ancestry Test",
            "GIT_COMMITTER_EMAIL": "ancestry@example.test",
        },
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.fixture
def commit_graph(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """创建合并前、真实双亲合并、后续提交和分叉，无需改动工作树文件。"""

    run_git(tmp_path, "init", "--quiet").check_returncode()
    tree_result = run_git(tmp_path, "hash-object", "-w", "-t", "tree", "--stdin")
    tree_result.check_returncode()
    tree = tree_result.stdout.strip()

    def commit(message: str, *parents: str) -> str:
        """以独立消息生成拓扑节点，避开本机提交钩子和签名配置。"""

        arguments = ["commit-tree", tree, "-m", message]
        for parent in parents:
            arguments.extend(["-p", parent])
        result = run_git(tmp_path, *arguments)
        result.check_returncode()
        return result.stdout.strip()

    before = commit("before")
    source = commit("source", before)
    merged = commit("merged", before, source)
    return tmp_path, {
        "before": before,
        "merged": merged,
        "head": commit("later", merged),
        "divergent": commit("divergent", before),
    }


def test_rendered_prompt_preserves_fixed_ancestry_direction() -> None:
    """渲染后应保留完整变量及固定方向，不让反向命令进入执行模板。"""

    assert ancestry_commands() == [
        ["git", "merge-base", "--is-ancestor", "$MERGE_BEFORE_SHA", "$MERGE_AFTER_SHA"],
        ["git", "merge-base", "--is-ancestor", "$MERGE_AFTER_SHA", "$TARGET_HEAD_AT_START"],
    ]
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    for contract in (
        "退出码 `0`：该项祖先关系校验通过",
        "退出码 `1`：该项祖先关系不成立",
        "其他退出码：Git 命令执行异常",
        "祖先关系无法验证",
        "$?",
        "$LASTEXITCODE",
        "未执行的项注明“未执行”",
    ):
        assert contract in prompt


@pytest.mark.parametrize(
    ("target", "expected_exit_code"),
    [("head", 0), ("merged", 0), ("divergent", 1)],
)
def test_prompt_commands_distinguish_target_history(
    commit_graph: tuple[Path, dict[str, str]], target: str, expected_exit_code: int
) -> None:
    """直接执行模板，覆盖正常推进、目标等于合并点和真正分叉。"""

    repository, commits = commit_graph
    values = {
        "$MERGE_BEFORE_SHA": commits["before"],
        "$MERGE_AFTER_SHA": commits["merged"],
        "$TARGET_HEAD_AT_START": commits[target],
    }
    commands = ancestry_commands()
    assert len(commands) == 2
    for command, expected in zip(commands, (0, expected_exit_code), strict=True):
        arguments = [values.get(argument, argument) for argument in command[1:]]
        result = run_git(repository, *arguments)
        assert result.returncode == expected, result.stderr
        assert result.stderr == ""


def test_reverse_failure_does_not_disprove_forward_ancestry(
    commit_graph: tuple[Path, dict[str, str]],
) -> None:
    """复现现场反向为一、正向为零的组合，不能据此断言历史改写。"""

    repository, commits = commit_graph
    for ancestor, descendant in (("before", "merged"), ("merged", "head")):
        forward = run_git(
            repository, "merge-base", "--is-ancestor", commits[ancestor], commits[descendant]
        )
        reverse = run_git(
            repository, "merge-base", "--is-ancestor", commits[descendant], commits[ancestor]
        )
        assert forward.returncode == 0, forward.stderr
        assert reverse.returncode == 1, reverse.stderr


def test_missing_commit_is_command_error_not_negative_ancestry(
    commit_graph: tuple[Path, dict[str, str]],
) -> None:
    """不存在的完整对象必须保留命令异常，不能与退出码一合并处理。"""

    repository, commits = commit_graph
    command = ancestry_commands()[1]
    values = {
        "$MERGE_AFTER_SHA": "0" * len(commits["merged"]),
        "$TARGET_HEAD_AT_START": commits["head"],
    }
    result = run_git(
        repository, *(values.get(argument, argument) for argument in command[1:])
    )
    assert result.returncode not in (0, 1)
    assert result.stderr.strip()
