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

Teamwork 会根据当前仓库的 GitLab Provider API 地址，为根 Agent、子 Agent 和仓库准备进程自动补齐 `GITLAB_HOST`。例如 `https://gitlab.example.com:8443/api/v4` 对应 `gitlab.example.com:8443`，不会把 `/api/v4` 或 URL 中的凭据传给 CLI。这样即使使用临时 HOME、没有 glab 登录配置，也不会因缺少主机信息默认访问 `gitlab.com`。

自动值只进入进程和运行审计，不进入 Prompt，也不写回配置或修改本机登录态。显式配置仍按“仓库 > Agent > 全局 > 自动默认值”整项覆盖，包含空值和暴露开关。Token 的“进程”开关保持原样；若 `token_env` 使用自定义名称，仍需显式配置 CLI 识别的 `GITLAB_TOKEN`。`GITLAB_API_HOST`、API 协议与命令 `--hostname` 的显式设置不由本功能改写，需确保它们指向正确实例。

确认已安装；需要本机登录作为后备认证时执行：

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

开发验收可设置 `TEAMWORK_TEST_GLAB_ROUTING=1` 后执行 `python -m pytest tests/test_gitlab_cli_environment.py -q`。真实 glab 用例只使用无效测试 Token 和拒绝转发的本机代理，验证默认主机与自动补齐主机的差异，不访问真实 GitLab、不使用业务凭据。
