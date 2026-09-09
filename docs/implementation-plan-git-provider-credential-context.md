# Provider Token 原生 Git 凭证上下文实施方案

## 已实施

- 新增 `git_auth.py`，提供运行级 `GitCredentialContext` 和异步上下文环境。
- 修改 `workspace._run_git()`，让所有基础仓库 Git 操作继承凭证环境，并显示脱敏、有界的 stderr 摘要。
- Agent 执行器在基础工作区准备前解析仓库 Provider Token，并将凭证上下文同时提供给工作区 Git 和 Agent 子进程。
- 自动 Preflight、手动 CI、仓库默认分支预热和手动基础仓库初始化均启用相同凭证上下文。
- Preflight 环境支持显式合并 Git 凭证环境，Provider Token 不再因 CI 环境过滤而丢失。
- 新增临时 askpass、Token 不进入命令参数及 helper 清理测试。

## 验证

- `uv run pytest -q` 全量测试通过。
- 定向执行工作区、Preflight、仓库初始化和环境解析测试通过。
- 编译检查和 `git diff --check` 通过。

## 后续可选项

Windows TLS 后端仍由系统 Git 配置决定。本次改动只保证底层 Schannel/OpenSSL 错误安全可见；如果需要在界面中选择 `inherit`、`openssl` 或 `schannel`，应作为独立配置变更实现，避免服务静默修改用户的全局 Git 配置。
