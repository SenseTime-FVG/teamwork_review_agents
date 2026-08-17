# Teamwork Review Agents

这是一个面向 GitHub Pull Request 和 GitLab Merge Request 的轻量 Agent 编排服务。它定时读取变更请求状态，将前后快照转换为语义事件，再按照 YAML 规则启动不同的 Codex CLI Agent。

核心能力：

- GitHub 与 GitLab 统一的变更请求模型。
- 提交、状态、Draft、标签、审批、流水线和可合并状态变化检测。
- SQLite 事件收件箱、运行审计与幂等控制。
- 同一变更请求或工作目录上的写操作串行化。
- 使用 `codex exec --json` 运行 Agent。
- 通过 MCP `invoke_agent` 工具调用配置好的 sub-agent。
- sub-agent 白名单、递归深度、总调用次数和超时限制。
- 全局、仓库、Agent 三级环境变量与 `${{ENV_NAME}}` Prompt 渲染。
- FastAPI 常驻后台、配置热加载、React 管理界面和实时运行日志。

## 快速开始

```bash
python -m pip install -e '.[dev]'
cp config_example.yaml config.yaml
export GITHUB_TOKEN='...'
teamwork-review-agents validate
teamwork-review-agents scan-once
```

所有命令默认读取当前工作目录的 `config.yaml`。需要使用其他配置时，仍可通过 `-c /path/to/other.yaml` 覆盖。

持续运行：

```bash
teamwork-review-agents serve
```

启动后访问 [http://127.0.0.1:8080](http://127.0.0.1:8080)。管理界面可以编辑 Provider、仓库、全局与分层环境变量、Agent、sub-agent 白名单和 MR/PR 触发规则，也可以立即扫描、暂停调度并查看 Agent 日志和父子运行记录。

`serve` 本身是前台常驻进程。生产环境请交给 systemd、launchd 或容器平台保活，不要使用应用内 fork。可直接参考：

- [systemd 服务模板](deploy/teamwork-review-agents.service)
- [macOS launchd 模板](deploy/com.teamwork.review-agents.plist)

只检测变化但不启动 Agent：

```bash
teamwork-review-agents scan-once --dry-run
```

首次看到一个 MR/PR 时，系统默认只建立基线，不触发 Agent。将 `scanner.emit_initial_events` 改为 `true` 后，可以让首次发现也产生 `change_request.discovered` 事件。

## 环境变量与 Prompt 模板

配置按“全局 → 仓库 → Agent → 运行时内置变量”合并，后面的同名变量覆盖前面的值：

```yaml
environment:
  global:
    ORGANIZATION_NAME: Example Team
    INTERNAL_API_TOKEN:
      from_system: TEAMWORK_INTERNAL_API_TOKEN
      secret: true
      expose_to_prompt: false
      expose_to_process: true

repositories:
  - id: example
    provider: github-main
    project: owner/repository
    workspace: ./workspaces/example
    environment:
      DEPLOY_ENV: staging

agents:
  reviewer:
    prompt: |
      请审查 ${{ORGANIZATION_NAME}} 的 #${{MR_NUMBER}}。
      未定义变量：${{NOT_DEFINED}}
    sandbox: read-only
    environment:
      REVIEW_STYLE: concise
```

模板中的未定义变量会渲染为空字符串。Secret 默认不进入 Prompt；配置 API、配置历史、渲染后的 Prompt、Codex JSONL 和 stderr 落库前都会脱敏。UI 中的 `********` 是保留原 Secret 的占位符。

系统还会注入仓库、MR/PR、事件与运行信息，包括 `REPOSITORY_ID`、`MR_NUMBER`、`MR_HEAD_SHA`、`MR_URL`、`EVENT_TYPE` 和 `RUN_ID`。

## 管理界面认证

默认只监听 `127.0.0.1`，可不配置管理 Token。只要监听非本机地址，就必须将 Token 放在宿主机环境变量中：

```yaml
web:
  host: 0.0.0.0
  port: 8080
  admin_token_env: TEAMWORK_ADMIN_TOKEN
```

```bash
export TEAMWORK_ADMIN_TOKEN='使用高强度随机值'
teamwork-review-agents serve
```

在 UI 顶部输入同一个 Token。Token 只保存在当前浏览器的 `localStorage`，不会写入 YAML。

## 前端开发

发布包已包含构建好的 UI。只有修改前端源码时才需要：

```bash
cd ui
npm install
npm run build
```

本地联调可以先启动 Python 后台，再在 `ui` 目录运行 `npm run dev`；Vite 会把 `/api` 转发到 `127.0.0.1:8080`。

## 运行前提

- 运行目录应当是对应仓库的本地 Git 工作目录。
- Codex CLI 已完成认证，或者运行环境提供仅对 `codex exec` 生效的认证方式。
- GitHub 使用 `GITHUB_TOKEN`，GitLab 使用配置中指定的 Token 环境变量。
- 需要进行 GitHub/GitLab 写操作的 Agent，可以使用本机已认证的 `gh`/`glab`，并应在提示词中明确操作边界。
- Provider Token 不会自动传进 Codex 子进程；写平台操作应使用单独的最小权限身份。

详细架构见 [系统设计](docs/design.md)，分阶段实施与验收项见 [实施方案](docs/implementation-plan.md)。
