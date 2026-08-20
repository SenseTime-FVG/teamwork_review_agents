"""跨平台目录清理工具测试。"""

from __future__ import annotations

import stat

from teamwork_review_agents import filesystem
from teamwork_review_agents.filesystem import remove_tree, temporary_directory


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
