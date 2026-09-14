# Windows 沙盒 curl 兼容设计

## 目标与边界

不新增 HTTP 函数或 MCP 工具。Windows 托管 Agent 继续通过现有命令工具使用真实 curl；macOS/Linux、完全访问 Agent 和宿主进程环境不变。Agent 不下载程序；缺少兼容程序时由服务部署阶段的[自动准备管理器](design-managed-curl-runtime.md)准备固定官方分发。不修改系统 PATH/ACL、不关闭证书验证、不在宿主重放失败命令。

## 运行级选择

新增可选 `runtime.managed_sandbox.curl_binary`。显式路径仅验证该程序，失败不偷偷替换；自动模式从服务已安装的 Git for Windows 和服务 PATH 寻找绝对路径候选，忽略当前目录和相对 PATH 项。候选必须是原生 `curl.exe`，只在现有外层沙盒中运行，不在宿主执行。

先执行禁用 curlrc 的版本探针，以 `CURL_SSL_BACKEND=openssl` 选择编译内置的 OpenSSL 后端，并检查实际选中的后端与 HTTPS 能力。Schannel-only 程序不能通过这个环境变量变成 OpenSSL 程序。选定程序及依赖安装目录仅只读授权，不能授权整个盘符。最多 8 个候选，共用不超过 30 秒、且不超过 Agent 总时限/空闲时限四分之一的异步探测预算；单进程时间和输出也有界，取消时回收整个进程树。

在原有网络策略允许联网且能确定仓库平台 HTTPS 主机时，进行无业务凭据的有界 HTTPS 探测；不新增域名白名单、不清除代理，保留 CA 配置。日志只记录程序路径、后端、来源、阶段、退出码与分类原因，不记录完整环境或原始探针错误。缺少兼容 curl 是可恢复的能力提示，不能阻断不使用 curl 的 Agent，也不触发模型回退或事件重试。

仅在当前 Agent 环境中将选定 curl 目录放在 PATH 前面，固定 TLS 后端。内嵌工具的 PowerShell 启动脚本将本次 `curl` 别名指向该真实程序；完整 CLI 的运行提示明确使用 `curl.exe` 或选定绝对路径，避开旧版 PowerShell 的内置别名。显式写死系统 curl 路径的命令不改写。选择结果及只读权限通过宿主内存中的运行上下文传递，嵌套与并发运行分别恢复。

## 故障分类

Git 预检可显式声明 Git 来源。普通命令只有在来源能确认为单条 Git 调用、或输出含 Git 专属 fatal 诊断时，才能提升为 `sandbox_git_*`；含混的复合脚本不依据泛化 TLS/HTTP 关键字推断来源。curl Schannel 失败保留为普通命令结果，附带兼容 curl / Python 标准库的恢复建议，不自动重放请求、不改为沙盒外执行。

## 验证

普通 CI 覆盖配置规范化、候选发现、多后端判断、运行级环境、CA/代理与网络策略保留、取消、两种 Runner 接入、curl 不触发 Git 终止、真实 Git 故障仍阻断。真实 Windows 沙盒提供显式启用验收，验证原生 curl 版本与 HTTPS；普通 CI 不冒充现场 ACL/TLS 验收。

参考：[curl TLS 后端与环境变量](https://curl.se/docs/manpage.html#CURL_SSL_BACKEND)。
