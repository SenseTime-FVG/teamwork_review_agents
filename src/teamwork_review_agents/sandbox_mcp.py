"""为 Windows 沙盒生成无需项目安装的可信 MCP 入口。"""

import base64
import zlib
from pathlib import Path

from .sandbox_python import current_sandbox_python


def standalone_mcp_command() -> list[str]:
    """压缩安装包中的固定源码以内联执行，避免命令过长与宿主读取可写代理脚本。"""

    source = Path(__file__).with_name("mcp_proxy_standalone.py").read_bytes()
    payload = base64.b64encode(zlib.compress(source)).decode("ascii")
    script = f"import base64,zlib;exec(compile(zlib.decompress(base64.b64decode({payload!r})), 'teamwork-mcp-proxy', 'exec'))"
    return [current_sandbox_python().executable, "-I", "-S", "-c", script]
