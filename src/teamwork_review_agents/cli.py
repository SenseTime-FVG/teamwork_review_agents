"""应用命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import uvicorn

from .config import load_config, validate_runtime_files
from .orchestrator import Orchestrator
from .process_manager import (
    ProcessActionResult,
    ServiceLease,
    resolve_config_path,
    start_background,
    stop_managed_process,
)
from .state import StateStore
from .webapp import create_app


DEFAULT_CONFIG_PATH = Path("config.yaml")


def _configure_standard_streams(*streams: Any) -> None:
    """统一使用 UTF-8 输出，避免 Windows 重定向控制台无法编码中文。"""

    targets = streams or (sys.stdout, sys.stderr)
    for stream in targets:
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # 已关闭或不允许重配的流继续沿用宿主环境设置。
            continue


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    """为子命令增加默认读取根目录 config.yaml 的参数。"""

    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="配置文件路径，默认：config.yaml",
    )


def add_server_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_managed_child: bool = False,
) -> None:
    """为前后台服务命令增加监听覆盖参数。"""

    add_config_argument(parser)
    parser.add_argument("--host", help="覆盖配置中的监听地址")
    parser.add_argument("--port", type=int, help="覆盖配置中的监听端口")
    if include_managed_child:
        parser.add_argument("--managed-child", action="store_true", help=argparse.SUPPRESS)


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

    run = subparsers.add_parser("run", help="在当前终端前台持续运行")
    add_server_arguments(run, include_managed_child=True)

    start = subparsers.add_parser("start", help="在后台启动服务")
    add_server_arguments(start)

    stop = subparsers.add_parser("stop", help="停止当前配置对应的服务")
    add_config_argument(stop)

    end = subparsers.add_parser("end", help="stop 的等价命令")
    add_config_argument(end)

    restart = subparsers.add_parser("restart", help="停止后重新在后台启动服务")
    add_server_arguments(restart)

    serve = subparsers.add_parser("serve", help="兼容命令，等同于 run")
    add_server_arguments(serve, include_managed_child=True)

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
    config_path = resolve_config_path(config_path)
    config = load_config(config_path)
    errors = validate_runtime_files(config)
    if errors:
        for error in errors:
            print(f"配置错误：{error}")
        return 2
    host = host_override or config.web.host
    port = port_override if port_override is not None else config.web.port
    if not 1 <= port <= 65535:
        print("配置错误：监听端口必须在 1 到 65535 之间")
        return 2
    if host not in {"127.0.0.1", "localhost", "::1"} and not config.web.admin_token_env:
        print("配置错误：监听非本机地址时必须配置 web.admin_token_env")
        return 2
    app = create_app(config_path)
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="info")
    )
    await server.serve()
    return 0


def _server_settings(
    config_path: Path,
    host_override: str | None,
    port_override: int | None,
) -> tuple[Path, str, int] | None:
    """校验服务配置并返回最终监听参数。"""

    resolved = resolve_config_path(config_path)
    config = load_config(resolved)
    errors = validate_runtime_files(config)
    if errors:
        for error in errors:
            print(f"配置错误：{error}")
        return None
    host = host_override or config.web.host
    port = port_override if port_override is not None else config.web.port
    if not 1 <= port <= 65535:
        print("配置错误：监听端口必须在 1 到 65535 之间")
        return None
    if host not in {"127.0.0.1", "localhost", "::1"} and not config.web.admin_token_env:
        print("配置错误：监听非本机地址时必须配置 web.admin_token_env")
        return None
    return resolved, host, port


def _run_server(
    config_path: Path,
    host_override: str | None,
    port_override: int | None,
    *,
    detached: bool,
) -> int:
    """申请单实例锁并在当前进程运行服务。"""

    settings = _server_settings(config_path, host_override, port_override)
    if settings is None:
        return 2
    resolved, host, port = settings
    lease = ServiceLease.acquire(
        resolved,
        host=host,
        port=port,
        detached=detached,
    )
    if lease is None:
        print("服务已在运行；如需重启请使用 teamwork-review-agents restart")
        return 3
    with lease:
        try:
            return asyncio.run(_serve(resolved, host, port))
        except KeyboardInterrupt:
            # 终端 Ctrl+C 与服务管理命令发出的停止请求都视为正常停机。
            return 0


def _print_process_result(result: ProcessActionResult) -> int:
    """输出进程管理结果并返回退出码。"""

    print(result.message)
    return result.exit_code


def main() -> None:
    """解析命令并返回适合脚本使用的退出码。"""

    _configure_standard_streams()
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
    if args.command in {"run", "serve"}:
        raise SystemExit(
            _run_server(
                args.config,
                args.host,
                args.port,
                detached=bool(args.managed_child),
            )
        )
    if args.command == "start":
        settings = _server_settings(args.config, args.host, args.port)
        if settings is None:
            raise SystemExit(2)
        resolved, host, port = settings
        raise SystemExit(
            _print_process_result(start_background(resolved, host=host, port=port))
        )
    if args.command in {"stop", "end"}:
        raise SystemExit(_print_process_result(stop_managed_process(args.config)))
    if args.command == "restart":
        settings = _server_settings(args.config, args.host, args.port)
        if settings is None:
            raise SystemExit(2)
        resolved, host, port = settings
        stop_result = stop_managed_process(resolved)
        if stop_result.exit_code:
            raise SystemExit(_print_process_result(stop_result))
        print(stop_result.message)
        raise SystemExit(
            _print_process_result(start_background(resolved, host=host, port=port))
        )
    if args.command == "runs":
        config = load_config(args.config)
        store = StateStore(config.database.path)
        store.initialize()
        print(json.dumps(store.list_runs(max(1, args.limit)), ensure_ascii=False, indent=2))
