# Windows 沙盒 Git HTTPS 兼容设计

## 边界

只处理 Windows 托管沙盒，不改变宿主 Git、Linux/macOS、完整访问 Agent 的行为。不新增沙盒外远端 Git 工具，不关闭证书校验，不清除网络代理、不扩大联网权限。OpenAI Windows 沙盒使用独立低权限账户或受限令牌，不能假设其拥有宿主 Schannel / Credential Manager 上下文。

## 运行级配置

为单次运行复制进程环境，追加 `http.sslBackend=openssl` 和 `http.sslVerify=true`，合并已有 `GIT_CONFIG_COUNT` 和 excludes/凭据设置。用户显式提供的 CA 路径保持有效；没有可用 OpenSSL 或可信 CA 时明确阻断，不降为不验证证书。

仅当已有进程环境包含获准暴露的 Teamwork Git Token 时创建新的 askpass helper。该文件不含 Token，独立于宿主同步使用的 helper，位于独立随机运行目录。权限档案只添加 helper 与 Python 运行依赖的只读目录，不放行整个临时目录，不为业务工作区增加写权限。采用隔离 Python 启动，避免导入工作区同名模块。

目录权限通过宿主内存中的运行上下文传递，不从 Agent 可控的环境变量解析授权路径。嵌套调用和并发运行分别保存、恢复上下文；运行结束清理目录。

## 诊断和失败语义

在模型启动前用实际工具环境和同一沙盒执行 helper 无密钥自检、读取 origin，再对 HTTPS origin 执行有界的 `git ls-remote --exit-code origin HEAD`。禁网、SSH 或没有 origin 的运行不发起 HTTPS 探测。不得打印完整 URL、Token 或环境。

记录 TLS 后端、Git 路径、远端主机、阶段、SHA 和脱敏错误。明确区分 Schannel 无上下文、OpenSSL 后端缺失、CA/证书失败、helper 不可执行、认证失败、网络和超时。

确定性的基础设施错误使用 `sandbox_git_*` 错误码和 `retryable=false`。模型工具执行中若明确检测到这些 Git 基础设施故障，直接结束本轮，不能吞成普通工具反馈或交给模型回退。子 Agent 此类失败向父 Agent 传播。普通 Git 非零退出（例如分支不存在、冲突）仍按原工具结果处理。完整 CLI 模式启动前做相同探测，运行中通过已完成命令的 JSONL 错误识别基础设施失败并终止进程树，不重放 shell。

## 验证

单测覆盖配置合并、只读授权、Token 隔离、临时目录清理、禁网/SSH 跳过、故障分类、模型与 CLI 启动前阻断、子 Agent 传播和普通错误不误伤。Windows CI 执行不依赖宿主 Codex 登录的回归；实际 Windows 托管沙盒和企业 CA 仍需部署环境验收。

参考：[Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)、[Git SSL 配置](https://git-scm.com/docs/git-config#Documentation/git-config.txt-httpsslBackend)。
