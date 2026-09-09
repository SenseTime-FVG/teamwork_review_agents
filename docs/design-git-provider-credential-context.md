# Provider Token 原生 Git 凭证上下文设计

## 背景

Provider Token 目前可以按仓库、全局和宿主机环境解析，也可以按配置进入 Agent 或 CI 进程。但原生 Git 不会因为存在 `GITHUB_TOKEN` 或 `GITLAB_TOKEN` 环境变量就自动完成 HTTPS 认证，基础仓库在 Agent 环境解析前执行的 clone/fetch 因此无法使用仓库凭证。

## 目标行为

- 基础仓库的 clone、fetch、PR/MR 引用获取和默认分支更新，在执行前自动解析当前 Provider Token。
- 解析顺序保持仓库环境变量、全局环境变量、宿主机环境变量。
- Provider Token 是否勾选“进程”只控制 Agent、工具命令和 CI 是否直接读取该变量，不影响 Teamwork 自己管理的 Git 凭证。
- Agent、自动 CI、手动 CI 和仓库预热中的原生 Git 共用当前运行的凭证上下文。
- SSH 地址继续使用 SSH Agent 或密钥；没有 Token 的公开 HTTPS 仓库继续匿名访问。

## 凭证实现

有 Token 时，为单次运行建立临时 `GIT_ASKPASS` helper，并通过子进程环境传递 Token 和平台对应的用户名。Token 不进入 URL、命令参数、`.git-credentials`、日志、异常、运行快照或配置历史。Git 同时关闭终端交互，避免认证失败时长时间等待。

凭证上下文使用异步任务上下文变量，使 `asyncio.to_thread()` 中的工作区 Git 操作自动继承；上下文在运行结束、取消、超时和异常路径统一清理临时 helper。

## 错误诊断

Git stderr 经过控制字符清理、Token 脱敏和长度限制后写入结构化 Git 进度和运行错误。这样 Windows Schannel 的 `SEC_E_NO_CREDENTIALS` 等底层错误可以被看到，同时不会泄露凭证。TLS 后端不自动修改全局 Git 配置。

## 安全边界

Provider Token 作为 Teamwork 管理的 Git 凭证不受环境变量“进程”选项影响；该选项仍控制普通 Agent、工具和 CI 进程能否直接读取逻辑变量。临时 helper 只在运行期间存在，结束后删除。
