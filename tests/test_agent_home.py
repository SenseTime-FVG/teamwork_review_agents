"""Agent 临时 HOME 生命周期与凭据入口桥接测试。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import teamwork_review_agents.agent_home as agent_home_module
from teamwork_review_agents.agent_home import (
    TemporaryAgentHome,
    TemporaryCodexHome,
    agent_codex_home_root,
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

    repeated_environment: dict[str, str] = {}
    repeated_bridges = temporary_home.apply_environment(
        repeated_environment,
        codex_home=codex_home,
        host_environment={
            "HOME": str(host_home),
            "SSH_AUTH_SOCK": "/tmp/test-agent.sock",
        },
    )

    assert repeated_environment == environment
    assert repeated_bridges == bridges
    assert temporary_home.cleanup() is None
    assert not path.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows 不验证 macOS 目录链接")
def test_temporary_agent_home_bridges_macos_keychains(
    tmp_path, monkeypatch
) -> None:
    """macOS 临时 HOME 应只链接真实钥匙串目录并在清理时保留原目录。"""

    monkeypatch.setattr(agent_home_module.sys, "platform", "darwin")
    host_home = tmp_path / "host-home"
    host_keychains = host_home / "Library/Keychains"
    host_keychains.mkdir(parents=True)
    marker = host_keychains / "login.keychain-db"
    marker.write_text("保留", encoding="utf-8")
    codex_home = host_home / ".codex"
    codex_home.mkdir()

    temporary_home = TemporaryAgentHome.create(
        "run-keychain",
        root=tmp_path / "homes",
    )
    environment: dict[str, str] = {}
    bridges = temporary_home.apply_environment(
        environment,
        codex_home=codex_home,
        host_environment={"HOME": str(host_home)},
    )
    path = temporary_home.path
    keychain_bridge = path / "Library/Keychains"

    assert keychain_bridge.is_symlink()
    assert keychain_bridge.resolve() == host_keychains.resolve()
    assert "MACOS_KEYCHAINS" in bridges

    repeated_environment: dict[str, str] = {}
    repeated_bridges = temporary_home.apply_environment(
        repeated_environment,
        codex_home=codex_home,
        host_environment={"HOME": str(host_home)},
    )

    assert repeated_bridges == bridges
    assert keychain_bridge.is_symlink()
    assert keychain_bridge.resolve() == host_keychains.resolve()
    assert temporary_home.cleanup() is None
    assert not path.exists()
    assert marker.read_text(encoding="utf-8") == "保留"


@pytest.mark.skipif(os.name == "nt", reason="Windows 不验证 macOS 目录链接")
def test_temporary_agent_home_rejects_occupied_macos_keychain_bridge(
    tmp_path, monkeypatch
) -> None:
    """异常对象占用钥匙串桥接路径时不得自动覆盖或删除。"""

    monkeypatch.setattr(agent_home_module.sys, "platform", "darwin")
    host_home = tmp_path / "host-home"
    (host_home / "Library/Keychains").mkdir(parents=True)
    codex_home = host_home / ".codex"
    codex_home.mkdir()
    temporary_home = TemporaryAgentHome.create(
        "run-occupied-keychain",
        root=tmp_path / "homes",
    )
    occupied = temporary_home.path / "Library/Keychains"
    occupied.mkdir(parents=True)
    marker = occupied / "unexpected"
    marker.write_text("保留", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Keychain 桥接路径已被占用"):
        temporary_home.apply_environment(
            {},
            codex_home=codex_home,
            host_environment={"HOME": str(host_home)},
        )

    assert marker.read_text(encoding="utf-8") == "保留"
    assert temporary_home.cleanup() is None


@pytest.mark.skipif(os.name == "nt", reason="Windows 不验证 macOS 目录链接")
def test_temporary_agent_home_skips_missing_macos_keychains(
    tmp_path, monkeypatch
) -> None:
    """宿主钥匙串目录不存在时不应创建无效桥接或阻断 Agent。"""

    monkeypatch.setattr(agent_home_module.sys, "platform", "darwin")
    host_home = tmp_path / "host-home"
    codex_home = host_home / ".codex"
    codex_home.mkdir(parents=True)
    temporary_home = TemporaryAgentHome.create(
        "run-no-keychain",
        root=tmp_path / "homes",
    )
    environment: dict[str, str] = {}

    bridges = temporary_home.apply_environment(
        environment,
        codex_home=codex_home,
        host_environment={"HOME": str(host_home)},
    )

    assert "MACOS_KEYCHAINS" not in bridges
    assert not (temporary_home.path / "Library/Keychains").exists()
    assert temporary_home.cleanup() is None


@pytest.mark.skipif(os.name == "nt", reason="Windows 不验证 macOS 目录链接")
def test_temporary_agent_home_does_not_bridge_keychains_on_other_platforms(
    tmp_path, monkeypatch
) -> None:
    """非 macOS 平台即使存在同名目录也应保持原有临时 HOME 行为。"""

    monkeypatch.setattr(agent_home_module.sys, "platform", "linux")
    host_home = tmp_path / "host-home"
    (host_home / "Library/Keychains").mkdir(parents=True)
    codex_home = host_home / ".codex"
    codex_home.mkdir()
    temporary_home = TemporaryAgentHome.create(
        "run-linux",
        root=tmp_path / "homes",
    )
    environment: dict[str, str] = {}

    bridges = temporary_home.apply_environment(
        environment,
        codex_home=codex_home,
        host_environment={"HOME": str(host_home)},
    )

    assert "MACOS_KEYCHAINS" not in bridges
    assert not (temporary_home.path / "Library/Keychains").exists()
    assert temporary_home.cleanup() is None


def test_temporary_agent_home_repeats_windows_user_directories(
    tmp_path, monkeypatch
) -> None:
    """原生 Windows 用户目录重定向应可连续应用且结果稳定。"""

    monkeypatch.setattr(agent_home_module, "_is_windows", lambda: True)
    monkeypatch.setattr(agent_home_module.sys, "platform", "win32")
    host_home = tmp_path / "windows-user"
    codex_home = host_home / ".codex"
    codex_home.mkdir(parents=True)
    temporary_home = TemporaryAgentHome.create(
        "run-windows",
        root=tmp_path / "homes",
    )

    first_environment: dict[str, str] = {}
    first_bridges = temporary_home.apply_environment(
        first_environment,
        codex_home=codex_home,
        host_environment={"USERPROFILE": str(host_home)},
    )
    second_environment: dict[str, str] = {}
    second_bridges = temporary_home.apply_environment(
        second_environment,
        codex_home=codex_home,
        host_environment={"USERPROFILE": str(host_home)},
    )

    assert first_environment == second_environment
    assert first_bridges == second_bridges
    assert first_environment["USERPROFILE"] == str(temporary_home.path)
    assert first_environment["APPDATA"] == str(
        temporary_home.path / "AppData/Roaming"
    )
    assert first_environment["LOCALAPPDATA"] == str(
        temporary_home.path / "AppData/Local"
    )
    assert first_environment["TEMP"] == str(temporary_home.path / "tmp")
    assert first_environment["TMP"] == str(temporary_home.path / "tmp")
    assert (temporary_home.path / "AppData/Roaming").is_dir()
    assert (temporary_home.path / "AppData/Local").is_dir()
    assert temporary_home.cleanup() is None


def test_temporary_agent_home_bridges_windows_cli_logins(
    tmp_path, monkeypatch
) -> None:
    """Windows 临时用户目录应继续引用宿主 gh 与 glab 登录配置。"""

    monkeypatch.setattr(agent_home_module, "_is_windows", lambda: True)
    host_home = tmp_path / "windows-user"
    host_appdata = host_home / "AppData/Roaming"
    gh_config = host_appdata / "GitHub CLI"
    glab_config = host_appdata / "glab-cli"
    gh_config.mkdir(parents=True)
    glab_config.mkdir(parents=True)
    codex_home = host_home / ".codex"
    codex_home.mkdir()
    temporary_home = TemporaryAgentHome.create(
        "run-windows-cli",
        root=tmp_path / "homes",
    )
    environment: dict[str, str] = {}

    bridges = temporary_home.apply_environment(
        environment,
        codex_home=codex_home,
        host_environment={
            "USERPROFILE": str(host_home),
            "APPDATA": str(host_appdata),
        },
    )

    assert environment["GH_CONFIG_DIR"] == str(gh_config)
    assert environment["GLAB_CONFIG_DIR"] == str(glab_config)
    assert {"GH_CONFIG_DIR", "GLAB_CONFIG_DIR"}.issubset(bridges)
    assert temporary_home.cleanup() is None


@pytest.mark.parametrize(
    ("platform_name", "os_name", "environment", "expected"),
    [
        (
            "darwin",
            "posix",
            {"HOME": "/Users/tester"},
            Path(
                "/Users/tester/Library/Caches/"
                "teamwork-review-agents/agent-codex-homes"
            ),
        ),
        (
            "linux",
            "posix",
            {"HOME": "/home/tester", "XDG_CACHE_HOME": "/cache/tester"},
            Path("/cache/tester/teamwork-review-agents/agent-codex-homes"),
        ),
        (
            "linux",
            "posix",
            {"HOME": "/home/tester", "WSL_DISTRO_NAME": "Ubuntu"},
            Path("/home/tester/.cache/teamwork-review-agents/agent-codex-homes"),
        ),
        (
            "win32",
            "nt",
            {
                "USERPROFILE": "/windows/users/tester",
                "LOCALAPPDATA": "/windows/local/tester",
            },
            Path("/windows/local/tester/teamwork-review-agents/agent-codex-homes"),
        ),
    ],
)
def test_agent_codex_home_root_is_platform_specific(
    platform_name: str,
    os_name: str,
    environment: dict[str, str],
    expected: Path,
) -> None:
    """三平台运行目录应落在宿主缓存而不是系统临时目录。"""

    assert agent_codex_home_root(
        environment,
        platform_name=platform_name,
        os_name=os_name,
    ) == expected


def test_temporary_codex_home_copies_only_runtime_inputs(tmp_path) -> None:
    """每轮 Codex 目录只复制认证与配置，不复制共享状态。"""

    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    expected_files = {
        "auth.json": "测试认证",
        ".credentials.json": "测试凭据",
        "config.toml": 'model = "test"',
        "requirements.toml": "allowed = true",
    }
    for name, content in expected_files.items():
        (source_home / name).write_text(content, encoding="utf-8")
    (source_home / "state_5.sqlite").write_text("不得复制", encoding="utf-8")
    (source_home / "skills").mkdir()
    (source_home / "skills/marker").write_text("不得复制", encoding="utf-8")

    temporary_home = TemporaryCodexHome.create(
        "run-codex-runtime",
        source_home=source_home,
        root=tmp_path / "runtime-roots",
    )
    environment: dict[str, str] = {}
    bridges = temporary_home.apply_environment(environment)

    assert environment["CODEX_HOME"] == str(temporary_home.path)
    assert set(bridges) == {
        "CODEX_AUTH",
        "CODEX_CREDENTIALS",
        "CODEX_CONFIG",
        "CODEX_REQUIREMENTS",
    }
    assert {
        path.name for path in temporary_home.path.iterdir()
    } == set(expected_files)
    for name, content in expected_files.items():
        assert (temporary_home.path / name).read_text(encoding="utf-8") == content
        if os.name != "nt":
            assert stat.S_IMODE((temporary_home.path / name).stat().st_mode) == 0o600
    assert not (temporary_home.path / "state_5.sqlite").exists()
    assert not (temporary_home.path / "skills").exists()

    repeated_environment: dict[str, str] = {}
    assert temporary_home.apply_environment(repeated_environment) == bridges
    assert repeated_environment == environment

    (temporary_home.path / "auth.json").write_text("本轮更新", encoding="utf-8")

    runtime_path = temporary_home.path
    assert temporary_home.cleanup() is None
    assert not runtime_path.exists()
    assert (source_home / "auth.json").read_text(encoding="utf-8") == "测试认证"


def test_temporary_codex_home_supports_missing_auth_and_config(tmp_path) -> None:
    """API Key 或平台认证场景不要求宿主 Codex 文件一定存在。"""

    source_home = tmp_path / "empty-codex"
    source_home.mkdir()
    temporary_home = TemporaryCodexHome.create(
        "run-empty-codex",
        source_home=source_home,
        root=tmp_path / "runtime-roots",
    )

    assert temporary_home.bridges == ()
    assert list(temporary_home.path.iterdir()) == []
    assert temporary_home.cleanup() is None


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


@pytest.mark.skipif(os.name == "nt", reason="Windows 不验证 macOS 目录链接")
def test_stale_agent_home_cleanup_does_not_follow_keychain_bridge(tmp_path) -> None:
    """回收异常退出的临时 HOME 时不得跟随链接删除真实钥匙串。"""

    root = tmp_path / "homes"
    stale = root / "99999999-111111111111-run-stale-keychain"
    bridge_parent = stale / "Library"
    bridge_parent.mkdir(parents=True)
    host_keychains = tmp_path / "host-home/Library/Keychains"
    host_keychains.mkdir(parents=True)
    marker = host_keychains / "login.keychain-db"
    marker.write_text("保留", encoding="utf-8")
    (bridge_parent / "Keychains").symlink_to(
        host_keychains,
        target_is_directory=True,
    )

    removed = cleanup_stale_agent_homes(root)

    assert removed == [stale]
    assert not stale.exists()
    assert marker.read_text(encoding="utf-8") == "保留"
