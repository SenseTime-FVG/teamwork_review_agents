# 配置 `gh` / `glab`

Teamwork 的 Provider Token 默认进入 Agent 进程，`gh` / `glab` 会优先使用当前仓库解析出的 `GITHUB_TOKEN` / `GITLAB_TOKEN`。CLI 可执行文件仍安装在本机；管理员关闭 Token 的“进程”暴露或 Token 不可用时，才依赖启动 Teamwork 服务的同一系统用户保存的 CLI 登录态。建议完成下面的登录检查，把它作为后备认证。

## GitHub：`gh`

确认已安装并登录：

```bash
gh --version
gh auth login
gh auth status --hostname github.com
gh api user
```

如果使用 GitHub Enterprise，请在登录和状态检查时指定实际主机名：

```bash
gh auth login --hostname github.example.com
gh auth status --hostname github.example.com
gh api user --hostname github.example.com
```

官方参考：[登录](https://cli.github.com/manual/gh_auth_login)、[检查登录状态](https://cli.github.com/manual/gh_auth_status)、[调用 API](https://cli.github.com/manual/gh_api)。

## GitLab：`glab`

确认已安装并登录：

```bash
glab version
glab auth login
glab auth status --hostname gitlab.com
glab api user --hostname gitlab.com
```

如果使用自建 GitLab，请在登录和状态检查时指定实际主机名：

```bash
glab auth login --hostname gitlab.example.com
glab auth status --hostname gitlab.example.com
glab api user --hostname gitlab.example.com
```

官方参考：[登录](https://docs.gitlab.com/cli/auth/login/)、[检查登录状态](https://docs.gitlab.com/cli/auth/status/)、[调用 API](https://docs.gitlab.com/cli/api/)。

以上状态检查和 API 请求成功后，再启动或手动重启 Teamwork 服务。
