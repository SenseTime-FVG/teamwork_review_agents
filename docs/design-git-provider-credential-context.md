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

## 2026-09-14：凭据程序启动与检查诊断修正

实测发现，宿主上下文将 Python 路径和脚本参数拼接后赋给 `GIT_ASKPASS`，原生 Git 会把整串当作可执行文件路径，导致 Token 已解析但凭据程序无法启动。沙盒上下文已有正确的脚本入口，两者应复用同一生成逻辑。

- `GIT_ASKPASS` 只保存临时启动脚本的绝对路径；Python 路径、隔离参数及 helper 路径在脚本内部转义。POSIX 和 Git for Windows 均使用带 shebang 的脚本，保留空格、中文等路径支持。
- Python helper 使用当前运行所选解释器，并以 `-I -S` 隔离启动，不依赖 PATH 中的其他 Python 或工作区导入内容；脚本内不写 Token。
- 创建 helper 中途失败也清理本次目录；运行正常结束、超时、取消和异常继续由上下文统一清理，不修改用户 Git 配置。
- 原生 Git 错误在截断之前统一脱敏：当前凭据及常见编码形式、URL 内用户名密码和查询参数、认证头及控制字符。工作区日志和向导检查共用相同规则。
- 一键配置显示已脱敏的 `WorkspaceError`，保留凭据程序启动、权限、网络、TLS 和超时等具体线索；未知异常与平台 API 异常继续使用固定提示，不原样回传第三方响应。
- 不修改 Token 权限、仓库配置、TLS 后端或现有 15 秒检查时限；不承诺 API 仓库读取成功等同于 Git 读写权限。
