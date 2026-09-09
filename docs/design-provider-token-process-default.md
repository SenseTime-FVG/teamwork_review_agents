# Provider Token 默认进入运行进程设计

## 背景

GitHub/GitLab 平台连接已经按仓库解析各自的 Provider Token，但旧默认不会把该 Token 传给 Agent、工具命令或仓库 CI。这样会让 Agent 执行本机 `gh` / `glab` 时回退到宿主机 CLI 登录态，而该身份可能没有目标仓库所需权限，造成后台扫描成功、Agent 平台操作失败的不一致体验。

## 目标行为

- Provider Token 始终强制标记为 Secret，配置历史、运行快照和日志继续脱敏。
- Provider Token 默认不进入 Prompt，模型上下文不能直接读取明文凭据。
- Provider Token 默认进入运行进程，使 Agent、模型工具命令和仓库 CI 使用当前仓库解析出的平台身份。
- 仓库环境变量继续覆盖全局同名变量，Agent 环境变量继续覆盖仓库同名变量。
- `gh` / `glab` 仍使用宿主机安装的可执行文件；环境中的 `GITHUB_TOKEN` / `GITLAB_TOKEN` 只负责覆盖其认证身份。
- 管理员可以关闭“进程”开关；明确保存为 `false` 的现有配置不被加载器或升级过程改写。

## 默认值与兼容性

当变量名命中任一平台 Provider 的 `token_env` 时：

- 缺少 `secret`：补为 `true`；已有值也强制为 `true`。
- 缺少 `expose_to_prompt`：补为 `false`。
- 缺少 `expose_to_process`：补为 `true`。
- 已明确声明 `expose_to_prompt` 或 `expose_to_process`：保留原值。

普通变量新改名为 Provider Token，或新增平台 Provider 后使现有同名变量首次成为 Provider Token 时，采用 Provider 凭据的新默认：Prompt 关闭、进程开启。

## 安全边界

开启进程暴露意味着受信任的 Agent 命令、工具及仓库 CI 可以读取和使用 Token。该风险通过 Secret 脱敏、独立工作区、Agent 权限配置和管理员可关闭开关控制。Prompt 暴露仍是独立的高风险操作，保持默认关闭并继续要求显式确认。
