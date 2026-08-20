"""仓库级依赖下载缓存目录与安全环境。"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .config import AppConfig, RepositoryConfig
from .models import stable_hash


def repository_cache_root(
    config: AppConfig,
    repository: RepositoryConfig,
) -> Path:
    """返回只由当前仓库共享的稳定缓存根目录。"""

    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", repository.id).strip("-.")
    prefix = (safe_id or "repository")[:48]
    identity = stable_hash(repository.id, repository.provider, repository.project)[:12]
    return config.database.path.parent / "preflight-cache" / f"{prefix}-{identity}"


def _cache_directories(root: Path) -> dict[str, Path]:
    """返回常用生态的独立缓存目录，避免工具之间互相覆盖。"""

    return {
        "xdg": root / "xdg",
        "uv": root / "uv",
        "pip": root / "pip",
        "poetry": root / "poetry",
        "pdm": root / "pdm",
        "npm": root / "npm",
        "pnpm": root / "pnpm-store",
        "yarn": root / "yarn",
        "corepack": root / "corepack",
        "bun": root / "bun",
        "cargo": root / "cargo",
        "sccache": root / "sccache",
        "go-build": root / "go-build",
        "go-mod": root / "go-mod",
        "maven": root / "maven",
        "gradle": root / "gradle",
        "nuget": root / "nuget",
        "composer": root / "composer",
        "playwright": root / "playwright",
        "puppeteer": root / "puppeteer",
        "deno": root / "deno",
    }


def build_repository_cache_environment(root: Path) -> dict[str, str]:
    """创建仓库缓存目录并返回不含认证信息的工具缓存变量。"""

    resolved = root.expanduser().resolve()
    directories = _cache_directories(resolved)
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(resolved, 0o700)
    except OSError:
        # Windows 等平台可能不支持 POSIX 权限位，目录隔离仍由用户身份保证。
        pass

    maven_option = f'-Dmaven.repo.local="{directories["maven"]}"'
    return {
        "TEAMWORK_REPOSITORY_CACHE_DIR": str(resolved),
        # 保留旧变量，避免现有仓库脚本和已落地的 Preflight 配置失效。
        "TEAMWORK_PREFLIGHT_CACHE_DIR": str(resolved),
        "XDG_CACHE_HOME": str(directories["xdg"]),
        "UV_CACHE_DIR": str(directories["uv"]),
        "PIP_CACHE_DIR": str(directories["pip"]),
        "POETRY_CACHE_DIR": str(directories["poetry"]),
        "PDM_CACHE_DIR": str(directories["pdm"]),
        "NPM_CONFIG_CACHE": str(directories["npm"]),
        "npm_config_cache": str(directories["npm"]),
        "NPM_CONFIG_STORE_DIR": str(directories["pnpm"]),
        "npm_config_store_dir": str(directories["pnpm"]),
        "YARN_CACHE_FOLDER": str(directories["yarn"]),
        "COREPACK_HOME": str(directories["corepack"]),
        "BUN_INSTALL_CACHE_DIR": str(directories["bun"]),
        "CARGO_HOME": str(directories["cargo"]),
        "SCCACHE_DIR": str(directories["sccache"]),
        "GOCACHE": str(directories["go-build"]),
        "GOMODCACHE": str(directories["go-mod"]),
        "MAVEN_OPTS": maven_option,
        "GRADLE_USER_HOME": str(directories["gradle"]),
        "NUGET_PACKAGES": str(directories["nuget"]),
        "COMPOSER_CACHE_DIR": str(directories["composer"]),
        "PLAYWRIGHT_BROWSERS_PATH": str(directories["playwright"]),
        "PUPPETEER_CACHE_DIR": str(directories["puppeteer"]),
        "DENO_DIR": str(directories["deno"]),
    }
