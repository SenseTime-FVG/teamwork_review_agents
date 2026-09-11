# Windows 沙盒 Git HTTPS 兼容设计

## 边界

只处理 Windows 托管沙盒，不改变宿主 Git、Linux/macOS、完整访问 Agent 的行为。不新增沙盒外远端 Git 工具，不关闭证书校验，不清除网络代理、不扩大联网权限。OpenAI Windows 沙盒使用独立低权限账户或受限令牌，不能假设其拥有宿主 Schannel / Credential Manager 上下文。

## 运行级配置

为单次运行复制进程环境，追加 `http.sslBackend=openssl` 和 `http.sslVerify=true`，合并已有 `GIT_CONFIG_COUNT` 和 excludes/凭据设置。用户显式提供的 CA 路径保持有效；没有可用 OpenSSL 或可信 CA 时明确阻断，不降为不验证证书。

仅当已有进程环境包含获准暴露的 Teamwork Git Token 时创建新的 askpass helper。执行器在准备步骤前创建本轮独立的 Git 运行目录，在其 `git-askpass/` 子目录写入不含 Token 的 helper。只将该子目录作为精确可写根，Python 运行依赖仍为只读；不放行整个临时目录，不为业务工作区增加写权限。采用隔离 Python 启动，避免导入工作区同名模块。

Windows 部署实测发现只读授权的新建 helper 目录仍被 ACL 拒绝，而可写根授权能使同沙盒自检通过；这不是官方保证所有版本均有的行为。现有 Codex 运行目录由 Runner 稍后创建，晚于工作区准备，因此本次使用独立 Git 运行目录，不迁移两个 Runner 的生命周期或混入 Codex 登录文件。

可写 helper 只能在沙盒内执行。Windows Git 上下文启用时，沙盒外 MCP Broker 从宿主进程环境及独立 `GitCredentialContext` 构造环境，绝不继承工具环境的 askpass、Git 配置、HOME、PATH 或 Python 导入路径；子 Agent 的宿主 Git 仍使用受保护的凭据文件。无 Token 时也必须隔离，防止匿名 Git 通过环境回退执行父 Agent 改写的 helper。

目录权限通过宿主内存中的运行上下文传递，不从 Agent 可控的环境变量解析授权路径。嵌套调用和并发运行分别保存、恢复上下文；成功、失败、取消后均先结束工具/Broker，再清理 helper 和本轮 Git 运行目录。清理使用原始目录入口、不 resolve 可被修改的链接，不跟随符号链接或 Windows junction 删除其他目录。

Windows junction 不是 `Path.is_symlink()` 识别的普通符号链接，Python 3.12 的 `rmtree()` 会拒绝把 junction 当作顶层待删目录。清理器在每次重试前通过 `lstat` 的 reparse 属性和 mount-point tag 识别 junction，调用 `rmdir` 仅删除联接入口；普通符号链接使用 `unlink`，实际目录才使用 `rmtree`。使用 Python 3.11 已支持的元数据，不依赖 3.12 新增的 `Path.is_junction()`。目标缺失的失效联接也能识别；权限错误处理不对链接目标回退执行 chmod。新增原生 Windows 顶层、嵌套和失效 junction 回归，不跳过原有失败用例。

删除语义参考：[Windows RemoveDirectory](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-removedirectoryw)。

## 工作区所有权信任

Windows 上工作区创建者与沙盒运行用户可能不同，因此 TLS 自检前还需要通过 Git 的所有权校验。执行器在工作区创建或父工作区继承校验完成后，将实际运行目录作为必需参数传给 `SandboxGitContext`，不使用报错中的路径或模型声明的路径。

上下文规范化该目录的绝对路径，并在命令作用域依次追加 `safe.directory=` 与 `safe.directory=<本轮目录>`。空值重置来自系统、全局或已注入环境的信任列表，包括 `*`；重复键必须保留顺序。只清空本轮进程的信任列表，不改任何 Git 配置文件、owner 或 ACL，也不改变操作系统沙盒权限。OpenSSL、证书、凭据、excludes 与代理等原有配置继续合并。

准备步骤、两种模型运行器和工具进程共用该环境。子 Agent 创建新目录时重新生成单目录信任，继承父工作区时使用同一已校验目录；并发上下文不共享可变环境。日志记录本轮信任目录。若仍出现 `detected dubious ownership`，分类为 `sandbox_git_ownership_mismatch`，展示本地工作区所有权错误并停止确定性重试，不能误判为 HTTPS 或 Token 故障。

回归使用 Git 的异主测试模式，在不修改操作系统账户和目录 owner 的情况下验证：当前 clone 与 linked worktree 可读 origin/HEAD、其他异主仓库仍被拒绝、临时 HOME 与继承的通配信任不影响单目录限制。该设置不是额外的操作系统安全边界；拥有命令执行能力的 Agent 仍受原有沙盒文件权限约束。

## 诊断和失败语义

在模型启动前用实际工具环境和同一沙盒执行 helper 无密钥自检、读取 origin，再对 HTTPS origin 执行有界的 `git ls-remote --exit-code origin HEAD`。禁网、SSH 或没有 origin 的运行不发起 HTTPS 探测。不得打印完整 URL、Token 或环境。

记录 TLS 后端、Git 路径、远端主机、阶段、SHA 和脱敏错误。明确区分 Schannel 无上下文、OpenSSL 后端缺失、CA/证书失败、helper 不可执行、认证失败、网络和超时。

确定性的基础设施错误使用 `sandbox_git_*` 错误码和 `retryable=false`。模型工具执行中若明确检测到这些 Git 基础设施故障，直接结束本轮，不能吞成普通工具反馈或交给模型回退。子 Agent 此类失败向父 Agent 传播。普通 Git 非零退出（例如分支不存在、冲突）仍按原工具结果处理。完整 CLI 模式启动前做相同探测，运行中通过已完成命令的 JSONL 错误识别基础设施失败并终止进程树，不重放 shell。

## 验证

单测覆盖配置合并、精确可写授权、Python 只读依赖、Token 隔离、宿主 Broker 隔离、真实 Git credential fill 链路、临时目录清理、禁网/SSH 跳过、故障分类、模型与 CLI 启动前阻断、子 Agent 传播和普通错误不误伤。Windows CI 执行不依赖宿主 Codex 登录的回归；实际 Windows 托管沙盒和企业 CA 仍需部署环境验收。

参考：[Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)、[Git SSL 配置](https://git-scm.com/docs/git-config#Documentation/git-config.txt-httpsslBackend)。

目录信任语义参考：[Git safe.directory](https://git-scm.com/docs/git-config#Documentation/git-config.txt-safedirectory)。
