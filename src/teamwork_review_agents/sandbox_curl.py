"""仅为 Windows 托管运行选择真实 OpenSSL curl，不提供新的 HTTP 工具。"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .subprocess_utils import remove_environment_names, selected_environment


Probe = Callable[[list[str], dict[str, str]], Awaitable[dict[str, Any]]]
_ACTIVE: ContextVar[SandboxCurlContext | None] = ContextVar("sandbox_curl_context", default=None)
_UNAVAILABLE = (
    "本轮兼容 curl 暂不可用，请在全局配置的 Windows HTTPS 运行环境查看自动准备状态和具体原因。"
    "需要 HTTPS 时也可通过现有命令工具使用沙盒 Python 的 urllib.request；"
    "不要关闭证书校验或改为沙盒外执行。"
)


@dataclass(frozen=True)
class CurlCandidate:
    """候选来自宿主安装目录，路径只用于沙盒执行与只读依赖授权。"""

    executable: Path
    source: str
    ca_bundle: Path | None = None

    @property
    def readable_directories(self) -> tuple[Path, ...]:
        """保留动态库和 Git 随附 CA 的读取权限，不授权整个安装盘。"""

        roots = [self.executable.parent]
        if self.ca_bundle is not None:
            roots.append(self.ca_bundle.parent)
        return tuple(dict.fromkeys(roots))


def current_sandbox_curl() -> SandboxCurlContext | None:
    """运行级状态不从模型可控的环境变量读取。"""

    return _ACTIVE.get()


def curl_candidates(configured: Path | None, host: Mapping[str, str], managed_root: Path | None = None) -> list[CurlCandidate]:
    """优先校验后的项目缓存，再检测 Git 安装和宿主 PATH；不搜索相对路径。"""

    if configured is not None:
        return [CurlCandidate(configured.expanduser().resolve(), "configured_path")]
    managed_candidates: list[CurlCandidate] = []
    if managed_root is not None:
        from .curl_distribution import installed_candidate

        if installed := installed_candidate(managed_root):
            managed_candidates.append(CurlCandidate(installed[0], "managed_cache", installed[1]))
    environment = selected_environment({"PATH", "PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"}, host)
    path_directories = [Path(item.strip('"')) for item in environment.get("PATH", "").split(os.pathsep)
                        if item and Path(item.strip('"')).is_absolute()]
    git_roots: list[Path] = []
    for directory in path_directories:
        if (directory / "git.exe").is_file():
            git_roots.extend((directory.parent, directory.parent.parent))
    for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if value := environment.get(key):
            root = Path(value)
            if root.is_absolute():
                git_roots.append(root / ("Programs/Git" if key == "LOCALAPPDATA" else "Git"))
    candidates: list[CurlCandidate] = managed_candidates
    for root in dict.fromkeys(git_roots):
        for architecture in ("mingw64", "mingw32", "clangarm64", "ucrt64", "usr"):
            prefix = root / architecture
            ca_bundle = prefix / "etc" / "ssl" / "certs" / "ca-bundle.crt"
            candidates.append(CurlCandidate(prefix / "bin" / "curl.exe", "git_installation",
                                            ca_bundle if ca_bundle.is_file() else None))
    candidates.extend(CurlCandidate(path / "curl.exe", "service_path") for path in path_directories)
    found: dict[str, CurlCandidate] = {}
    for candidate in candidates:
        try:
            executable = candidate.executable.resolve(strict=True)
            if not executable.is_file() or executable.parent.parent == executable.parent:
                continue
            found.setdefault(str(executable).casefold(), CurlCandidate(
                executable, candidate.source,
                candidate.ca_bundle.resolve() if candidate.ca_bundle else None,
            ))
        except (OSError, ValueError, RuntimeError):
            continue
    return list(found.values())[:8]


def openssl_curl_version(output: str) -> str | None:
    """只接受实际启用的 OpenSSL 系列后端；括号中的可选后端不算通过。"""

    lines = output.splitlines()
    if not lines or not lines[0].startswith("curl "):
        return None
    active = re.sub(r"\([^)]*\)", "", lines[0])
    backend = re.search(r"\b(OpenSSL|LibreSSL|BoringSSL|AWS-LC)/[^\s]+", active)
    if backend is None or "Schannel" in active:
        return None
    protocols = next((line.partition(":")[2].split() for line in lines if line.startswith("Protocols:")), [])
    return backend.group() if "https" in protocols else None


def https_probe_url(base_url: str) -> str | None:
    """只探测已配置平台的 HTTPS 根地址，不携带 URL 凭据、查询或业务路径。"""

    try:
        parsed = urlsplit(base_url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            return None
        host = parsed.hostname.encode("idna").decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        return f"https://{host}{':' + str(parsed.port) if parsed.port else ''}/"
    except (ValueError, UnicodeError):
        return None


class SandboxCurlContext:
    """绑定本次运行的候选、验证结果与只读根；子运行结束恢复父上下文。"""

    def __init__(self, configured: Path | None, *, host_environment: Mapping[str, str] | None = None,
                 managed_root: Path | None = None) -> None:
        self.configured = configured
        self.managed_root = managed_root
        # 只保存发现所需的宿主字段，不保存宿主凭据或完整环境。
        self.host = selected_environment({"PATH", "PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"},
                                         os.environ if host_environment is None else host_environment)
        self.selected: CurlCandidate | None = None
        self.probing: CurlCandidate | None = None
        self.diagnostic: dict[str, Any] | None = None
        self._token: Token | None = None

    def start(self) -> SandboxCurlContext:
        """必须与执行器的 finally 成对使用。"""

        self._token = _ACTIVE.set(self)
        return self

    def close(self) -> None:
        """不创建安装文件，不修改全局环境，仅恢复运行上下文。"""

        if self._token is not None:
            _ACTIVE.reset(self._token)
            self._token = None
        self.probing = None

    @property
    def readable_directories(self) -> tuple[Path, ...]:
        """探测时只授权当前候选，结束后只保留被选程序。"""

        candidate = self.probing or self.selected
        return candidate.readable_directories if candidate else ()

    def apply_environment(self, environment: Mapping[str, str], candidate: CurlCandidate | None = None) -> dict[str, str]:
        """仅改本轮副本；保留已有 CA 与代理，不把宿主变量补入 Agent。"""

        result = dict(environment)
        candidate = candidate or self.selected
        if candidate is None:
            return result
        existing = selected_environment({"PATH", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR"}, result)
        remove_environment_names(result, {"PATH", "CURL_SSL_BACKEND"})
        directory = str(candidate.executable.parent)
        paths = [part for part in existing.get("PATH", "").split(os.pathsep)
                 if part and part.casefold() != directory.casefold()]
        result["PATH"] = os.pathsep.join((directory, *paths))
        result["CURL_SSL_BACKEND"] = "openssl"
        if candidate.ca_bundle is not None and not any(existing.get(key) for key in ("CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR")):
            remove_environment_names(result, {"CURL_CA_BUNDLE"})
            result["CURL_CA_BUNDLE"] = str(candidate.ca_bundle)
        return result

    async def prepare(self, probe: Probe, environment: Mapping[str, str], *, probe_url: str | None,
                      network_access: bool, budget_seconds: float = 30) -> dict[str, Any]:
        """可选 curl 能力共用总预算，避免多个坏候选拖垮本来无需 HTTP 的任务。"""

        try:
            async with asyncio.timeout(budget_seconds):
                return await self._prepare(probe, environment, probe_url=probe_url, network_access=network_access)
        except TimeoutError:
            if self.selected is not None:
                return self._https_warning("https_probe_budget_exhausted")
            self.diagnostic = {"status": "unavailable", "error_code": "sandbox_curl_unavailable",
                               "message": _UNAVAILABLE, "reason": "probe_budget_exhausted"}
            return self.diagnostic

    def _https_warning(self, reason: str, exit_code: int | None = None) -> dict[str, Any]:
        """网络阶段失败保留已验证程序，包括探针启动错误和总预算超时。"""

        self.diagnostic = {**(self.diagnostic or {}), "status": "warning", "https_probe": "failed",
                           "reason": reason, "exit_code": exit_code,
                           "message": "兼容 curl 程序已就绪，但本轮 HTTPS 探测失败；请检查 Agent 网络权限、代理或可信 CA，无需重装 OpenSSL。"}
        return self.diagnostic

    async def _prepare(self, probe: Probe, environment: Mapping[str, str], *, probe_url: str | None, network_access: bool) -> dict[str, Any]:
        """在真实工具沙盒验证；固定失败只提示，不触发 Agent 或模型重试。"""

        if self.diagnostic is not None:
            return self.diagnostic
        failures: list[dict[str, Any]] = []
        try:
            candidates = curl_candidates(self.configured, self.host, self.managed_root)
        except (OSError, ValueError, RuntimeError):
            candidates = []
        for candidate in candidates:
            failure: dict[str, Any] = {"path": str(candidate.executable), "source": candidate.source, "stage": "version"}
            failures.append(failure)
            try:
                if (candidate.executable.name.lower() != "curl.exe" or not candidate.executable.is_file()
                        or candidate.executable.parent.parent == candidate.executable.parent):
                    failure["reason"] = "invalid_executable"
                    continue
                self.probing = candidate
                child = self.apply_environment(environment, candidate)
                result = await probe([str(candidate.executable), "-q", "--version"], child)
                backend = openssl_curl_version(result["stdout"])
                if result["exit_code"] != 0 or result["timed_out"] or backend is None:
                    failure.update(reason="timeout" if result["timed_out"] else "openssl_curl_unavailable",
                                   exit_code=result["exit_code"])
                    continue
                self.selected = candidate
                self.diagnostic = {"status": "ready", "curl_binary": str(candidate.executable),
                                   "source": candidate.source, "ssl_backend": backend, "https_probe": "skipped"}
                if network_access and probe_url:
                    failure["stage"] = "https"
                    result = await probe([
                        str(candidate.executable), "-q", "--head", "--silent", "--show-error",
                        "--connect-timeout", "5", "--max-time", "10", "--output", "NUL",
                        "--write-out", "teamwork-curl-ready:%{http_code}", "--url", probe_url,
                    ], child)
                    if (result["exit_code"] != 0 or result["timed_out"]
                            or not re.fullmatch(r"teamwork-curl-ready:[1-5]\d{2}", result["stdout"].strip())):
                        # 程序已验证，网络/CA/策略失败不能再伪装成缺少 OpenSSL 或换程序重试。
                        return self._https_warning("https_probe_failed", result["exit_code"])
                    self.diagnostic["https_probe"] = "passed"
                return self.diagnostic
            except OSError as exc:
                if self.selected is candidate:
                    return self._https_warning("https_probe_launch_failed")
                failure.update(reason="sandbox_launch_failed", winerror=getattr(exc, "winerror", None))
            finally:
                self.probing = None
        self.diagnostic = {"status": "unavailable", "error_code": "sandbox_curl_unavailable",
                           "message": _UNAVAILABLE, "candidates": failures}
        return self.diagnostic

    def runtime_hint(self) -> str:
        """只提供命令环境事实，不向模型增加新的工具或泄露完整环境。"""

        if self.selected is None:
            from .sandbox_python import current_sandbox_python

            # 已验证解释器的绝对路径避免模型再次选中无沙盒执行权限的服务 Python。
            return f"{_UNAVAILABLE} 本轮沙盒 Python：{current_sandbox_python().executable}。"
        return (
            f"Windows HTTPS 命令环境：已选择 OpenSSL curl：{self.selected.executable}。"
            "请使用 curl.exe 或该绝对路径；不要使用 System32 下的 Schannel curl，"
            "也不要依赖 Windows PowerShell 的 curl（Invoke-WebRequest）别名。"
            "证书校验、代理和原有网络权限仍然生效。"
        )


def http_tls_hint(output: str) -> str | None:
    """普通 HTTP 命令只附恢复提示，绝不升级为 Git 故障或自动重放请求。"""

    lowered = output.lower()
    if "schannel" in lowered and ("sec_e_no_credentials" in lowered or "acquirecredentialshandle failed" in lowered):
        context = current_sandbox_curl()
        return context.runtime_hint() if context is not None else _UNAVAILABLE
    return None
