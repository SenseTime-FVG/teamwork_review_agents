"""应用命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import uvicorn

from .config import load_config, validate_runtime_files
from .orchestrator import Orchestrator
from .state import StateStore
from .webapp import create_app


DEFAULT_CONFIG_PATH = Path("config.yaml")


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    """为子命令增加默认读取根目录 config.yaml 的参数。"""

    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="配置文件路径，默认：config.yaml",
    )


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""

    parser = argparse.ArgumentParser(
        prog="teamwork-review-agents",
        description="定时扫描 GitHub/GitLab 变更请求并编排 Codex CLI Agent",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="校验配置和本地文件")
    add_config_argument(validate)

    scan_once = subparsers.add_parser("scan-once", help="执行一次扫描与事件处理")
    add_config_argument(scan_once)
    scan_once.add_argument(
        "--dry-run",
        action="store_true",
        help="保存快照和变化事件，但不启动 Agent",
    )

    serve = subparsers.add_parser("serve", help="按配置间隔持续运行")
    add_config_argument(serve)
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)

    runs = subparsers.add_parser("runs", help="查看最近 Agent 运行摘要")
    add_config_argument(runs)
    runs.add_argument("--limit", type=int, default=20)
    return parser


async def _scan_once(config_path: Path, dry_run: bool) -> int:
    config = load_config(config_path)
    errors = validate_runtime_files(config)
    if errors:
        for error in errors:
            print(f"配置错误：{error}")
        return 2
    summary = await Orchestrator(config).run_once(dry_run=dry_run)
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return 1 if summary.errors else 0


async def _serve(
    config_path: Path,
    host_override: str | None,
    port_override: int | None,
) -> int:
    config = load_config(config_path)
    errors = validate_runtime_files(config)
    if errors:
        for error in errors:
            print(f"配置错误：{error}")
        return 2
    host = host_override or config.web.host
    port = port_override or config.web.port
    if host not in {"127.0.0.1", "localhost", "::1"} and not config.web.admin_token_env:
        print("配置错误：监听非本机地址时必须配置 web.admin_token_env")
        return 2
    app = create_app(config_path)
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="info")
    )
    await server.serve()
    return 0


def main() -> None:
    """解析命令并返回适合脚本使用的退出码。"""

    args = build_parser().parse_args()
    if args.command == "validate":
        config = load_config(args.config)
        errors = validate_runtime_files(config)
        if errors:
            for error in errors:
                print(f"配置错误：{error}")
            raise SystemExit(2)
        print("配置校验通过")
        return
    if args.command == "scan-once":
        raise SystemExit(asyncio.run(_scan_once(args.config, args.dry_run)))
    if args.command == "serve":
        try:
            exit_code = asyncio.run(_serve(args.config, args.host, args.port))
        except KeyboardInterrupt:
            # 终端 Ctrl+C 与服务管理器 SIGINT 都视为正常停机。
            exit_code = 0
        raise SystemExit(exit_code)
    if args.command == "runs":
        config = load_config(args.config)
        store = StateStore(config.database.path)
        store.initialize()
        print(json.dumps(store.list_runs(max(1, args.limit)), ensure_ascii=False, indent=2))
