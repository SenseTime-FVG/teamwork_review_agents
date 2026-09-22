# GitLab CLI 默认主机设计

## 问题与目标

`glab` 会读取 `GITLAB_TOKEN`，但未登记的自建 GitLab 主机可能被忽略；临时 HOME 没有登录配置时，`glab api` 会退回 `gitlab.com`。Token 有效也不能替代目标主机配置。

为绑定 GitLab Provider 的仓库自动补齐进程变量 `GITLAB_HOST`，不新增工具，不修改真实部署配置或用户 CLI 登录态。

## 环境解析

- 从当前仓库 Provider 的 `base_url` 提取主机和可选端口，不携带 API 路径、用户信息、查询参数或 Token。
- 只接受带主机的 HTTP/HTTPS API 地址；无效地址明确报错，错误信息不回显原始 URL。
- 自动值作为最低优先级默认定义。显式环境配置继续按“仓库 > Agent > 全局”整项覆盖，包含空值、宿主引用和暴露开关。
- 自动值进入进程和脱敏审计，不主动加入 Prompt；不修改 `GITLAB_TOKEN` 的来源和暴露权限，不自动复制其他名称的凭据。
- 根 Agent、子 Agent 和仓库准备复用统一解析；每次根据当前仓库计算，不写入共享环境或配置。不改变本地 CI 当前仅支持 GitHub 的限制。
- 未绑定 Provider 的占位仓库仍可用于 Prompt 预览，不生成 CLI 主机；真实运行的 Provider 引用由现有配置校验保证。
- GitHub 仓库不注入 GitLab 默认变量；显式设置的 GitLab 变量仍保持原有行为。

## 范围与限制

本次只补齐默认主机，不重写 Agent 命令，不代替 glab 安装、Token 续期、访问权限或 API 协议配置。用户显式覆盖 `GITLAB_HOST`、`GITLAB_API_HOST` 或命令 `--hostname` 时仍需保证目标正确。

## 验证

覆盖端口、IPv6、URL 凭据不泄露、多仓库隔离、分层覆盖、Token 关闭进程暴露，以及临时 HOME 和沙盒环境传递。使用真实 glab 的无凭据本机代理探针验证修复前后目标主机，不向外部发送测试请求或真实 Token。
