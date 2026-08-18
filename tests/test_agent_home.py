"""Agent 临时 HOME 生命周期与凭据入口桥接测试。"""

from __future__ import annotations

import os
from pathlib import Path

from teamwork_review_agents.agent_home import (
    TemporaryAgentHome,
    cleanup_stale_agent_homes,
)


def test_temporary_agent_home_redirects_generic_user_directories(tmp_path) -> None:
    """仓库程序使用的通用用户目录应全部落入本次运行目录。"""

    host_home = tmp_path / "host-home"
    gh_config = host_home / ".config/gh"
    glab_config = host_home / ".config/glab-cli"
    codex_home = host_home / ".codex"
    gh_config.mkdir(parents=True)
    glab_config.mkdir(parents=True)
    codex_home.mkdir()
    git_config = host_home / ".gitconfig"
    git_config.write_text("[user]\n\tname = Test\n", encoding="utf-8")

    temporary_home = TemporaryAgentHome.create("run-123", root=tmp_path / "homes")
    environment: dict[str, str] = {}
    bridges = temporary_home.apply_environment(
        environment,
        codex_home=codex_home,
        host_environment={
            "HOME": str(host_home),
            "SSH_AUTH_SOCK": "/tmp/test-agent.sock",
        },
    )
    path = temporary_home.path

    assert environment["HOME"] == str(path)
    assert environment["XDG_CACHE_HOME"] == str(path / ".cache")
    assert environment["XDG_CONFIG_HOME"] == str(path / ".config")
    assert environment["XDG_DATA_HOME"] == str(path / ".local/share")
    assert environment["XDG_STATE_HOME"] == str(path / ".local/state")
    assert environment["CODEX_HOME"] == str(codex_home)
    assert environment["GH_CONFIG_DIR"] == str(gh_config)
    assert environment["GLAB_CONFIG_DIR"] == str(glab_config)
    assert environment["GIT_CONFIG_GLOBAL"] == str(git_config)
    assert environment["SSH_AUTH_SOCK"] == "/tmp/test-agent.sock"
    assert set(bridges) == {
        "CODEX_HOME",
        "GH_CONFIG_DIR",
        "GLAB_CONFIG_DIR",
        "GIT_CONFIG_GLOBAL",
        "SSH_AUTH_SOCK",
    }
    assert temporary_home.cleanup() is None
    assert not path.exists()


def test_stale_agent_home_cleanup_keeps_live_owner(tmp_path) -> None:
    """启动回收只删除失联进程的目录，不碰仍有存活属主的运行。"""

    root = tmp_path / "homes"
    root.mkdir()
    stale = root / "99999999-111111111111-run-stale-value"
    live = root / f"{os.getpid()}-222222222222-run-live-value"
    unrelated = root / "unrelated"
    stale.mkdir()
    live.mkdir()
    unrelated.mkdir()

    removed = cleanup_stale_agent_homes(root)

    assert removed == [stale]
    assert not stale.exists()
    assert live.exists()
    assert unrelated.exists()

