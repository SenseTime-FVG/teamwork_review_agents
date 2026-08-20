"""Codex Skill 元数据、受管目录导入与运行时投影。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

import yaml

from .filesystem import remove_tree


MAX_SKILL_FILES = 512
MAX_SKILL_FILE_BYTES = 8 * 1024 * 1024
MAX_SKILL_TOTAL_BYTES = 32 * 1024 * 1024
MAX_SKILL_MD_BYTES = 1024 * 1024
_FRONTMATTER_PATTERN = re.compile(
    r"\A---[ \t]*\r?\n(?P<header>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)


@dataclass(frozen=True)
class SkillMetadata:
    """从 SKILL.md 头部读取的展示元数据。"""

    name: str
    description: str


def skill_directory(config_path: str | Path) -> Path:
    """返回配置文件旁由管理 UI 导入 Skill 的目录。"""

    return Path(config_path).expanduser().resolve().parent / "skills"


def _display_path(config_path: str | Path, path: Path) -> str:
    """把受管目录转换为适合写入 YAML 的相对路径。"""

    config_directory = Path(config_path).expanduser().resolve().parent
    relative = path.resolve().relative_to(config_directory)
    return f"./{relative.as_posix()}"


def read_skill_metadata(path: str | Path) -> SkillMetadata:
    """校验 Skill 目录并读取 SKILL.md 的 name 与 description。"""

    directory = Path(path).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError(f"Skill 路径不是文件夹：{directory}")
    manifest = directory / "SKILL.md"
    if not manifest.is_file():
        raise ValueError(f"Skill 目录缺少 SKILL.md：{directory}")
    if manifest.stat().st_size > MAX_SKILL_MD_BYTES:
        raise ValueError("SKILL.md 不能超过 1 MiB")
    try:
        content = manifest.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SKILL.md 必须使用 UTF-8 编码") from exc
    match = _FRONTMATTER_PATTERN.match(content.lstrip("\ufeff"))
    if match is None:
        raise ValueError("SKILL.md 必须以 YAML frontmatter 开头")
    try:
        header = yaml.safe_load(match.group("header")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"SKILL.md frontmatter 不是有效 YAML：{exc}") from exc
    if not isinstance(header, dict):
        raise ValueError("SKILL.md frontmatter 必须是对象")
    name = str(header.get("name") or "").strip()
    description = str(header.get("description") or "").strip()
    if not name:
        raise ValueError("SKILL.md frontmatter 缺少 name")
    if not description:
        raise ValueError("SKILL.md frontmatter 缺少 description")
    if len(name) > 128 or any(character in name for character in "\r\n\0"):
        raise ValueError("SKILL.md 的 name 非法或超过 128 个字符")
    if len(description) > 4096 or "\0" in description:
        raise ValueError("SKILL.md 的 description 非法或超过 4096 个字符")
    return SkillMetadata(name=name, description=description)


def list_skill_directories(config_path: str | Path) -> list[dict[str, Any]]:
    """列出受管 skills 目录下可以加入配置的 Skill。"""

    directory = skill_directory(config_path)
    if not directory.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_dir() or path.is_symlink() or path.name.startswith("."):
            continue
        try:
            metadata = read_skill_metadata(path)
            result.append(
                {
                    "directory": path.name,
                    "path": _display_path(config_path, path),
                    "name": metadata.name,
                    "description": metadata.description,
                    "valid": True,
                    "error": None,
                }
            )
        except ValueError as exc:
            result.append(
                {
                    "directory": path.name,
                    "path": _display_path(config_path, path),
                    "name": path.name,
                    "description": "",
                    "valid": False,
                    "error": str(exc),
                }
            )
    return result


def inspect_skill_path(config_path: str | Path, value: str | Path) -> dict[str, Any]:
    """解析任意服务端 Skill 路径并返回元数据。"""

    config_directory = Path(config_path).expanduser().resolve().parent
    expanded = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if not expanded.is_absolute():
        expanded = config_directory / expanded
    resolved = expanded.resolve()
    metadata = read_skill_metadata(resolved)
    return {
        "path": str(value),
        "resolved_path": str(resolved),
        "name": metadata.name,
        "description": metadata.description,
        "valid": True,
        "error": None,
    }


def _safe_relative_path(filename: str) -> PurePosixPath:
    """校验浏览器上传的相对路径，拒绝绝对路径和目录穿越。"""

    normalized = filename.replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise ValueError(f"Skill 文件路径非法：{filename}")
    return path


def _safe_directory_name(name: str) -> str:
    """根据 Skill 名称生成受管目录名。"""

    value = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._").lower()
    return value[:80] or "skill"


def import_skill_directory(
    config_path: str | Path,
    files: Iterable[tuple[str, bytes]],
) -> dict[str, Any]:
    """校验浏览器文件夹上传并原子复制到受管 skills 目录。"""

    items = list(files)
    if not items:
        raise ValueError("请选择一个包含 SKILL.md 的 Skill 文件夹")
    if len(items) > MAX_SKILL_FILES:
        raise ValueError(f"单个 Skill 最多包含 {MAX_SKILL_FILES} 个文件")

    normalized: list[tuple[PurePosixPath, bytes]] = []
    seen: set[PurePosixPath] = set()
    total_bytes = 0
    for filename, content in items:
        relative = _safe_relative_path(filename)
        if relative in seen:
            raise ValueError(f"Skill 中存在重复文件：{relative.as_posix()}")
        if len(content) > MAX_SKILL_FILE_BYTES:
            raise ValueError(f"Skill 单个文件不能超过 8 MiB：{relative.as_posix()}")
        total_bytes += len(content)
        if total_bytes > MAX_SKILL_TOTAL_BYTES:
            raise ValueError("单个 Skill 全部文件合计不能超过 32 MiB")
        seen.add(relative)
        normalized.append((relative, content))

    # 浏览器目录选择会把选中目录名作为第一层；兼容测试或 API 直接上传根文件。
    paths = [path for path, _ in normalized]
    if PurePosixPath("SKILL.md") in paths:
        stripped = normalized
    else:
        roots = {path.parts[0] for path in paths}
        if len(roots) != 1 or any(len(path.parts) < 2 for path in paths):
            raise ValueError("一次只能导入一个 Skill 文件夹")
        stripped = [
            (PurePosixPath(*path.parts[1:]), content)
            for path, content in normalized
        ]
    if PurePosixPath("SKILL.md") not in {path for path, _ in stripped}:
        raise ValueError("所选文件夹根目录缺少 SKILL.md")

    managed_directory = skill_directory(config_path)
    managed_directory.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".skill-import-", dir=managed_directory))
    target: Path | None = None
    try:
        for relative, content in stripped:
            destination = staging.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        metadata = read_skill_metadata(staging)
        target = managed_directory / _safe_directory_name(metadata.name)
        if target.exists():
            raise ValueError(f"受管 Skill 目录已存在：{target.name}")
        os.replace(staging, target)
        return {
            "directory": target.name,
            "path": _display_path(config_path, target),
            "name": metadata.name,
            "description": metadata.description,
            "valid": True,
            "error": None,
        }
    finally:
        if staging.exists():
            remove_tree(staging)


def _projection_name(skill_id: str, revision: str) -> str:
    """为投影目录生成稳定且不依赖用户输入安全性的名称。"""

    digest = hashlib.sha256(f"{revision}:{skill_id}".encode("utf-8")).hexdigest()[:10]
    suffix = _safe_directory_name(skill_id)[:48]
    return f"teamwork-{digest}-{suffix}"


class SkillProjection:
    """把应用 Skill 临时复制到 Codex 原生发现目录并负责清理。"""

    def __init__(self, workspace: Path, skills: Mapping[str, Path], revision: str) -> None:
        self.workspace = workspace.resolve()
        self.skills = dict(skills)
        self.revision = revision
        self.root = self.workspace / ".agents" / "skills"
        self.marker = self.root / f".teamwork-{revision[:16]}.projection"
        self.skill_files: dict[str, Path] = {
            skill_id: self.root / _projection_name(skill_id, revision) / "SKILL.md"
            for skill_id in self.skills
        }
        self._owned = False
        self._created: list[Path] = []

    def prepare(self) -> "SkillProjection":
        """创建本次工作区投影；继承工作区时复用父 Agent 的投影。"""

        if not self.skills:
            return self
        if self.marker.is_file():
            missing = [str(path) for path in self.skill_files.values() if not path.is_file()]
            if missing:
                raise RuntimeError(f"继承工作区中的 Skill 投影不完整：{missing}")
            return self

        self.root.mkdir(parents=True, exist_ok=True)
        try:
            for skill_id, source in sorted(self.skills.items()):
                read_skill_metadata(source)
                destination = self.skill_files[skill_id].parent
                if destination.exists():
                    raise RuntimeError(f"Skill 投影目录已经存在，拒绝覆盖：{destination}")
                shutil.copytree(
                    source,
                    destination,
                    symlinks=False,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
                )
                self._created.append(destination)
            # 同一文件也作为 Codex 子进程的 Git 全局忽略文件，避免投影进入状态或 add -A。
            self.marker.write_text(
                ".agents/skills/teamwork-*\n"
                ".agents/skills/.teamwork-*.projection\n",
                encoding="utf-8",
            )
            self._owned = True
            return self
        except Exception:
            self.cleanup(force=True)
            raise

    def cleanup(self, *, force: bool = False) -> None:
        """只删除当前进程创建的投影，不触碰仓库已有 Skill。"""

        if not self._owned and not force:
            return
        for path in reversed(self._created):
            if path.exists():
                remove_tree(path)
        if self.marker.exists() and (self._owned or force):
            self.marker.unlink()
        for directory in (self.root, self.root.parent):
            try:
                directory.rmdir()
            except OSError:
                break
        self._created.clear()
        self._owned = False
