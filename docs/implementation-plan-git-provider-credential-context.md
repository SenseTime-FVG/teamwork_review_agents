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

## 2026-09-14：凭据程序修正实施计划

1. 提取宿主和沙盒共用的 askpass 启动脚本生成器，保证环境变量仅包含一个可执行路径，并修复创建失败时的清理。
2. 集中处理 Git 错误脱敏和长度限制，在向导中展示已知 Git 故障摘要，保留未知异常的固定兜底提示。
3. 使用真实 `git credential fill` 和本地 HTTP 认证挑战测试凭据传递，不使用真实 Token 或外部私有仓库；覆盖中文空格路径、两种平台、匿名、并发上下文、失败和清理。
4. 将新增跨平台测试加入 Windows CI，运行凭据、向导、工作区、沙盒和相关执行器回归测试；本机无法运行的平台明确交由 CI 验证。
5. 执行 Python 编译检查与差异检查，记录实际结果。不修改真实配置，不在未授权时提交或推送。

### 本次实施与验证结果

- 已共用 `write_askpass_launcher()`，宿主入口不再把解释器及参数误当作文件名；新增创建失败清理，并保留沙盒独立目录及权限边界。
- 已共用 `safe_git_error_detail()`，向导、工作区异常和结构化进度都在输出前完成脱敏；实际 `/api/setup/check` 返回路径也有回归覆盖。
- 真实 Git 的凭据读取、本地 HTTP 认证挑战、GitHub/GitLab 用户名、包含中文/空格/引号的入口路径、匿名读取、失败/超时/取消清理与并发恢复均通过测试。
- macOS 执行 `.venv/bin/python -m pytest -o addopts='' -q`：867 项通过、8 项跳过；有 2 条现有 FastAPI/Starlette 依赖弃用警告。
- `.venv/bin/python -m compileall -q src tests` 和 `git diff --check` 通过。
- Windows CI 已加入 `tests/test_git_auth.py`、`tests/test_quick_setup.py`；本次未实跑 Windows，未使用真实 Token 验证远端私有仓库，也未重启正在运行的后台。
