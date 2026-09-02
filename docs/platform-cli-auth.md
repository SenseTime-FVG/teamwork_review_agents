# 配置 `gh` / `glab`

Teamwork 的 Provider Token 默认只供扫描器访问平台 API，不能自动代替 Agent 使用的 `gh` / `glab` 登录态。即使显式开启 Token 的进程暴露，仍建议为平台写操作使用启动 Teamwork 服务的同一系统用户完成下列 CLI 登录。

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
