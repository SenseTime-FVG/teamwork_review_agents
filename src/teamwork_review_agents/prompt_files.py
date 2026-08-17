"""管理 UI 导入的 Prompt 文本文件。"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any


ALLOWED_PROMPT_SUFFIXES = {".md", ".txt"}
MAX_PROMPT_FILE_BYTES = 1024 * 1024


def prompt_directory(config_path: str | Path) -> Path:
    """返回配置文件旁的受管 Prompt 目录。"""

    return Path(config_path).expanduser().resolve().parent / "prompts"


def _display_path(config_path: str | Path, path: Path) -> str:
    """返回适合写入 YAML 的配置相对路径。"""

    config_directory = Path(config_path).expanduser().resolve().parent
    relative = path.resolve().relative_to(config_directory)
    return f"./{relative.as_posix()}"


def list_prompt_files(config_path: str | Path) -> list[dict[str, Any]]:
    """列出受管目录下可供 Agent 选择的 Prompt 文件。"""

    directory = prompt_directory(config_path)
    if not directory.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        if (
            path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in ALLOWED_PROMPT_SUFFIXES
        ):
            files.append(
                {
                    "name": path.name,
                    "path": _display_path(config_path, path),
                    "size": path.stat().st_size,
                }
            )
    return files


def _safe_filename(filename: str) -> str:
    """清理浏览器提供的文件名并限制扩展名。"""

    basename = Path(filename.replace("\\", "/")).name
    suffix = Path(basename).suffix.lower()
    if suffix not in ALLOWED_PROMPT_SUFFIXES:
        raise ValueError("Prompt 文件只支持 .md 或 .txt")
    stem = re.sub(r"[^\w\-]+", "-", Path(basename).stem, flags=re.UNICODE).strip("-_")
    if not stem:
        stem = "prompt"
    return f"{stem}{suffix}"


def import_prompt_file(
    config_path: str | Path,
    filename: str,
    content: bytes,
) -> dict[str, Any]:
    """校验并将浏览器文件原子复制到受管 Prompt 目录。"""

    if not content:
        raise ValueError("Prompt 文件不能为空")
    if len(content) > MAX_PROMPT_FILE_BYTES:
        raise ValueError("Prompt 文件不能超过 1 MiB")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Prompt 文件必须使用 UTF-8 编码") from exc

    directory = prompt_directory(config_path)
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(filename)
    target = directory / safe_name
    if target.is_file() and not target.is_symlink() and target.read_bytes() == content:
        return {
            "name": target.name,
            "path": _display_path(config_path, target),
            "size": len(content),
        }

    index = 2
    while target.exists():
        target = directory / f"{Path(safe_name).stem}-{index}{Path(safe_name).suffix}"
        index += 1

    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=directory,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = file.name
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, target)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return {
        "name": target.name,
        "path": _display_path(config_path, target),
        "size": len(content),
    }
