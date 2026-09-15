"""固定官方 curl 分发的完整性校验、安全展开和项目级缓存。"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx

from .filesystem import remove_tree


_MAX_DOWNLOAD = 20 * 1024 * 1024
_MAX_EXPANDED = 128 * 1024 * 1024


class CurlPreparationError(RuntimeError):
    """准备失败只携带固定中文原因和错误码，不回显代理凭据或网络响应。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CurlDistribution:
    """版本、架构与摘要必须随代码审核更新，不从远端 latest 或配置动态接受。"""

    package: str
    sha256: str

    @property
    def filename(self) -> str:
        """离线包和在线包使用相同文件名。"""

        return f"{self.package}.zip"

    @property
    def url(self) -> str:
        """只允许 curl 官方固定版本地址。"""

        return f"https://curl.se/windows/dl-8.22.0_1/{self.filename}"


# 校验值取自官方 ZIP 摘要文件；不能在下载时再信任一个可同时被替换的摘要。
_DISTRIBUTIONS = {
    "amd64": CurlDistribution("curl-8.22.0_1-win64-mingw", "7f23b039f6ea4197362d4468e1a0e71428201222e1bef3b680d5ef7b2aefb714"),
    "arm64": CurlDistribution("curl-8.22.0_1-win64a-mingw", "3223726340eab447170004435bb1941081c9726b59c3a717a563d3287741e0b4"),
}


def distribution_for_machine(machine: str | None = None) -> CurlDistribution:
    """按实际进程架构选择包，不把未知或 32 位平台默认为 x64。"""

    architecture = (machine or platform.machine()).casefold()
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(architecture, architecture)
    if architecture not in _DISTRIBUTIONS:
        raise CurlPreparationError("unsupported_architecture", "该 Windows 架构暂无内置 curl 分发，请使用高级路径配置。")
    return _DISTRIBUTIONS[architecture]


def curl_runtime_root(config) -> Path:
    """缓存属于部署数据目录，不属于 Agent HOME 或工作区。"""

    return config.database.path.parent.resolve() / "runtimes" / "curl"


def require_plain_path(path: Path, *, directory: bool) -> None:
    """拒绝符号链接和 Windows reparse point，不通过缓存入口扩大读写范围。"""

    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise CurlPreparationError("unsafe_cache_path", "curl 缓存入口不是普通目录或文件，请检查目录链接和权限。")


def ensure_cache_directories(root: Path) -> None:
    """只创建固定缓存子目录；已存在的链接或其他对象不替换。"""

    for path in (root.parent, root, root / "downloads"):
        path.mkdir(exist_ok=True)
        require_plain_path(path, directory=True)


def read_verified_archive(path: Path, distribution: CurlDistribution) -> bytes:
    """离线、在线和缓存复用均经过同一个固定摘要校验。"""

    require_plain_path(path, directory=False)
    with path.open("rb") as source:
        data = source.read(_MAX_DOWNLOAD + 1)
    if len(data) > _MAX_DOWNLOAD:
        raise CurlPreparationError("archive_too_large", "curl 安装包超过允许体积，已拒绝使用。")
    if hashlib.sha256(data).hexdigest() != distribution.sha256:
        raise CurlPreparationError("archive_integrity_failed", "curl 安装包校验失败，已拒绝使用；请重试或提供对应版本的官方离线包。")
    return data


def archive_entries(archive: zipfile.ZipFile, distribution: CurlDistribution) -> list[tuple[zipfile.ZipInfo, Path]]:
    """展开前完整校验名称、链接、数量和体积，兼容 Windows 路径约束。"""

    entries = archive.infolist()
    if len(entries) > 1024 or sum(entry.file_size for entry in entries) > _MAX_EXPANDED:
        raise CurlPreparationError("unsafe_archive", "curl 安装包展开规模异常，已拒绝使用。")
    result: list[tuple[zipfile.ZipInfo, Path]] = []
    seen: set[str] = set()
    for entry in entries:
        # Windows 的 ZipInfo.filename 会把反斜杠规范化；安全校验必须使用归档原文。
        name = entry.orig_filename.rstrip("/")
        parts = name.split("/")
        mode = entry.external_attr >> 16
        if (not parts or parts[0] != distribution.package or "\\" in name or entry.flag_bits & 1
                or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFDIR, stat.S_IFREG})
                or any(not part or part in {".", ".."} or part.endswith((".", " "))
                       or any(character in part for character in ':<>"|?*')
                       or any(ord(character) < 32 for character in part)
                       or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
                       for part in parts)):
            raise CurlPreparationError("unsafe_archive", "curl 安装包包含不安全路径或链接，已拒绝展开。")
        if name.casefold() in seen:
            raise CurlPreparationError("unsafe_archive", "curl 安装包含重复路径，已拒绝展开。")
        seen.add(name.casefold())
        if len(parts) > 1:
            result.append((entry, Path(*PurePosixPath(name).parts[1:])))
    required = {"bin/curl.exe", "bin/curl-ca-bundle.crt", "COPYING.txt"}
    if not required.issubset({path.as_posix() for entry, path in result if not entry.is_dir()}):
        raise CurlPreparationError("unsafe_archive", "curl 安装包缺少程序、CA 或许可证文件。")
    return result


def installed_candidate(root: Path, distribution: CurlDistribution | None = None) -> tuple[Path, Path] | None:
    """用已固定摘要的归档验证安装二进制，不能只信任自写的 ready 标记。"""

    try:
        distribution = distribution or distribution_for_machine()
        target = root / distribution.package
        for path in (root.parent, root, root / "downloads", target, target / "bin"):
            require_plain_path(path, directory=True)
        data = read_verified_archive(root / "downloads" / distribution.filename, distribution)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive_entries(archive, distribution)
            files = [(entry, path) for entry, path in entries if not entry.is_dir() and path.parts[0] == "bin"]
            expected = {path.name for _, path in files}
            if {path.name for path in (target / "bin").iterdir()} != expected:
                return None
            for entry, relative in files:
                path = target / relative
                require_plain_path(path, directory=False)
                if path.stat().st_size != entry.file_size or hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(archive.read(entry)).digest():
                    return None
        return target / "bin" / "curl.exe", target / "bin" / "curl-ca-bundle.crt"
    except (OSError, ValueError, zipfile.BadZipFile, CurlPreparationError):
        return None


async def download_archive(root: Path, distribution: CurlDistribution) -> Path:
    """公共下载不附加业务认证、不跟随重定向，取消时只清理本次临时文件。"""

    destination = root / "downloads" / distribution.filename
    descriptor, temporary = tempfile.mkstemp(prefix=".download-", dir=root / "downloads")
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), follow_redirects=False) as client:
                async with client.stream("GET", distribution.url) as response:
                    response.raise_for_status()
                    size = 0
                    async for chunk in response.aiter_bytes(65536):
                        size += len(chunk)
                        if size > _MAX_DOWNLOAD:
                            raise CurlPreparationError("archive_too_large", "curl 下载超过允许体积，已停止。")
                        output.write(chunk)
        read_verified_archive(temporary_path, distribution)
        if destination.exists() or destination.is_symlink():
            # 另一部署操作刚放入离线包时保留并校验它，不覆盖未知文件。
            read_verified_archive(destination, distribution)
        else:
            temporary_path.replace(destination)
        return destination
    except httpx.HTTPError as exc:
        raise CurlPreparationError("download_failed", "curl 官方安装包下载失败，请检查服务网络/代理/可信证书，或放入离线包后重试。") from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def publish_archive(root: Path, distribution: CurlDistribution, archive_path: Path) -> tuple[Path, Path]:
    """固定目录锁内校验并原子发布，损坏旧目录隔离保存，不直接递归删除。"""

    data = read_verified_archive(archive_path, distribution)
    # Python 3.12.4+ 在 Windows 会将 tempfile.mkdtemp 的 0o700 转换为
    # 仅当前用户/管理员可访问的 ACL；沙盒受限令牌随后无法读取正式程序。
    # 使用普通 mkdir 继承缓存根目录的 ACL，Unix 仍保留私有 staging 权限。
    temporary = root / f".install-{uuid.uuid4().hex}"
    temporary.mkdir(mode=0o700 if os.name != "nt" else 0o755)
    require_plain_path(temporary, directory=True)
    target = root / distribution.package
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive_entries(archive, distribution)
            for entry, relative in entries:
                destination = temporary / relative
                if entry.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, destination.open("xb") as output:
                        while chunk := source.read(65536):
                            output.write(chunk)
        if target.exists() or target.is_symlink():
            require_plain_path(target, directory=True)
            target.rename(root / f".{distribution.package}.invalid-{uuid.uuid4().hex}")
        temporary.rename(target)
        return target / "bin" / "curl.exe", target / "bin" / "curl-ca-bundle.crt"
    except zipfile.BadZipFile as exc:
        raise CurlPreparationError("unsafe_archive", "curl 安装包格式损坏，已拒绝使用。") from exc
    finally:
        if temporary.exists():
            remove_tree(temporary)
