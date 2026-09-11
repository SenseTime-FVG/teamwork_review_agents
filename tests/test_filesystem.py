"""跨平台目录清理工具测试。"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from teamwork_review_agents import filesystem
from teamwork_review_agents.filesystem import remove_tree, temporary_directory
from teamwork_review_agents.process_control import hidden_process_options


def test_remove_tree_deletes_read_only_content(tmp_path) -> None:
    """Windows 只读 Git 对象不能阻止运行目录回收。"""

    target = tmp_path / "readonly-tree"
    nested = target / ".git/objects/pack"
    nested.mkdir(parents=True)
    packed = nested / "pack-test.pack"
    packed.write_bytes(b"git object")
    packed.chmod(stat.S_IREAD)

    remove_tree(target)

    assert not target.exists()


def test_remove_tree_retries_transient_os_error(tmp_path, monkeypatch) -> None:
    """短暂文件占用应有界重试，不能立即把正常运行标为保留。"""

    target = tmp_path / "transient-lock"
    target.mkdir()
    real_rmtree = filesystem.shutil.rmtree
    calls = 0

    def flaky_rmtree(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("文件暂时被占用")
        return real_rmtree(*args, **kwargs)

    monkeypatch.setattr(filesystem.shutil, "rmtree", flaky_rmtree)

    remove_tree(target, retry_delay_seconds=0)

    assert calls == 2
    assert not target.exists()


def test_temporary_directory_uses_portable_cleanup(tmp_path) -> None:
    """临时目录退出时也必须清理其中的只读文件。"""

    with temporary_directory(directory=tmp_path, prefix="portable-") as path:
        marker = path / "readonly.txt"
        marker.write_text("测试", encoding="utf-8")
        marker.chmod(stat.S_IREAD)
        retained_path = path

    assert not retained_path.exists()


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("dangling", [False, True])
def test_remove_tree_preserves_directory_link_target(tmp_path, nested, dangling) -> None:
    """原生 Windows 使用 junction，POSIX 使用符号链接；顶层和嵌套均不能删除目标。"""

    outside = tmp_path / "target with spaces"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("目标内容必须保留", encoding="utf-8")
    mode_before = outside.stat().st_mode
    root = tmp_path / "cleanup-root"
    if nested:
        root.mkdir()
        (root / "ordinary.txt").write_text("可以清理", encoding="utf-8")
    link = root / "link" if nested else root
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True, text=True, errors="replace", timeout=10,
            **hidden_process_options(),
        )
        assert result.returncode == 0, result.stderr
        assert filesystem._is_junction(link.lstat())
        assert not link.is_symlink()
    else:
        link.symlink_to(outside, target_is_directory=True)
    if dangling:
        # 移走目标使原联接失效，但保留内容用于确认没有被错误删除。
        relocated = tmp_path / "relocated-target"
        outside.rename(relocated)
        outside = relocated
        keep = outside / "keep.txt"
        assert not link.exists()

    remove_tree(root)

    assert not os.path.lexists(link)
    assert not root.exists()
    assert keep.read_text(encoding="utf-8") == "目标内容必须保留"
    assert outside.stat().st_mode == mode_before


def junction_info():
    """模拟 Windows lstat 元数据，让非 Windows CI 也覆盖相同分支。"""

    return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                           st_reparse_tag=0xA0000003)


@pytest.mark.parametrize("retries,fail_always", [(0, False), (2, False), (3, True)])
def test_junction_removal_uses_rmdir_and_bounded_retries(monkeypatch, retries, fail_always) -> None:
    """junction 删除失败只允许有限次 rmdir，不能递归、unlink 或 chmod 目标。"""

    target = Mock(spec=Path)
    target.lstat.return_value = junction_info()
    failures = [PermissionError("联接暂时被占用") for _ in range(retries)]
    target.rmdir.side_effect = failures if fail_always else [*failures, None]
    monkeypatch.setattr(filesystem, "Path", lambda _: target)
    recursive = Mock(side_effect=AssertionError("不能递归 junction"))
    chmod = Mock(side_effect=AssertionError("不能修改目标权限"))
    monkeypatch.setattr(filesystem.shutil, "rmtree", recursive)
    monkeypatch.setattr(filesystem.os, "chmod", chmod)
    if fail_always:
        with pytest.raises(PermissionError, match="联接暂时被占用"):
            remove_tree("owned-junction", retry_delay_seconds=0)
    else:
        remove_tree("owned-junction", retry_delay_seconds=0)
    assert target.rmdir.call_count == (3 if fail_always else retries + 1)
    assert target.lstat.call_count == target.rmdir.call_count
    target.unlink.assert_not_called()
    recursive.assert_not_called()
    chmod.assert_not_called()


def test_retry_rechecks_directory_replaced_by_junction(monkeypatch) -> None:
    """普通目录删除失败后若变成 junction，下一次必须改为只删除入口。"""

    target = Mock(spec=Path)
    target.lstat.side_effect = [SimpleNamespace(st_mode=stat.S_IFDIR), junction_info()]
    monkeypatch.setattr(filesystem, "Path", lambda _: target)
    recursive = Mock(side_effect=PermissionError("目录暂时被占用"))
    monkeypatch.setattr(filesystem.shutil, "rmtree", recursive)

    remove_tree("owned-directory", retry_delay_seconds=0)

    recursive.assert_called_once()
    target.rmdir.assert_called_once()


def test_nested_junction_permission_error_never_chmods_target(tmp_path, monkeypatch) -> None:
    """嵌套 junction 的错误回调也只能重试移除入口，不能触发跟随目标的权限回退。"""

    root = tmp_path / "root"
    root.mkdir()
    link = Mock(spec=Path)
    link.lstat.return_value = junction_info()
    monkeypatch.setattr(filesystem, "Path", lambda path: link if path == "nested-junction" else root)
    chmod = Mock(side_effect=AssertionError("不能修改 junction 目标权限"))
    monkeypatch.setattr(filesystem.os, "chmod", chmod)

    def recursive(path, *, onerror):
        error = PermissionError("嵌套联接暂时被占用")
        onerror(os.rmdir, "nested-junction", (PermissionError, error, None))

    monkeypatch.setattr(filesystem.shutil, "rmtree", recursive)
    remove_tree(root)
    link.rmdir.assert_called_once()
    chmod.assert_not_called()


def test_make_writable_rejects_junction_before_chmod(monkeypatch) -> None:
    """即使直接进入权限恢复函数，也不能把联接当成普通目录修改。"""

    target = Mock(spec=Path)
    target.lstat.return_value = junction_info()
    monkeypatch.setattr(filesystem, "Path", lambda _: target)
    chmod = Mock(side_effect=AssertionError("不能修改 junction 目标权限"))
    monkeypatch.setattr(filesystem.os, "chmod", chmod)
    with pytest.raises(PermissionError, match="拒绝修改目录链接目标"):
        filesystem._make_writable("owned-junction")
    chmod.assert_not_called()


def test_chmod_fallback_rechecks_junction(monkeypatch) -> None:
    """不支持无跟随 chmod 时，回退也必须拒绝刚被替换的联接入口。"""

    target = Mock(spec=Path)
    target.lstat.side_effect = [SimpleNamespace(st_mode=stat.S_IFDIR), junction_info()]
    monkeypatch.setattr(filesystem, "Path", lambda _: target)
    chmod = Mock(side_effect=NotImplementedError("模拟旧版 Windows chmod"))
    monkeypatch.setattr(filesystem.os, "chmod", chmod)
    with pytest.raises(NotImplementedError):
        filesystem._make_writable("owned-directory")
    chmod.assert_called_once()
    assert chmod.call_args.kwargs["follow_symlinks"] is False


@pytest.mark.parametrize("missing_ok", [False, True])
def test_child_disappearing_in_error_callback_preserves_cleanup_policy(tmp_path, monkeypatch, missing_ok) -> None:
    """错误回调发现子项已消失时，默认继续清理剩余项；严格模式则保留错误。"""

    root = tmp_path / "root"
    root.mkdir()
    remaining = root / "remaining"
    remaining.touch()

    def recursive(path, *, onerror):
        error = PermissionError("子项在报错后已被移除")
        onerror(os.unlink, root / "disappeared", (PermissionError, error, None))
        remaining.unlink()
        root.rmdir()

    monkeypatch.setattr(filesystem.shutil, "rmtree", recursive)
    if missing_ok:
        remove_tree(root)
        assert not root.exists()
    else:
        with pytest.raises(FileNotFoundError):
            remove_tree(root, missing_ok=False)
        assert remaining.exists()


@pytest.mark.parametrize("missing_ok", [False, True])
def test_remove_tree_preserves_missing_path_policy(tmp_path, missing_ok) -> None:
    """入口分类不能改变缺失路径的错误处理约定。"""

    missing = tmp_path / "missing"
    if missing_ok:
        remove_tree(missing)
    else:
        with pytest.raises(FileNotFoundError):
            remove_tree(missing, missing_ok=False)
