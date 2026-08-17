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
teamwork-review-agents validate
teamwork-review-agents serve
```

所有命令默认读取当前工作目录的 `config.yaml`。需要使用其他配置时，仍可通过 `-c /path/to/other.yaml` 覆盖。

全部配置项、默认值和字段用途见 [`config_example.yaml`](config_example.yaml)。其中的业务示例保持注释状态，复制后不会在首次 UI 中创建示例仓库、Agent 或规则。

首次启动时，管理 UI 中的 GitHub / GitLab 连接、仓库、环境变量、Agent 和触发规则都是空的。访问 [http://127.0.0.1:8080](http://127.0.0.1:8080) 后按实际需要添加配置并保存即可。

## 运行模式

### `serve`：持续后台运行

```bash
teamwork-review-agents serve
```

启动 FastAPI 管理服务和后台调度器。服务启动后立即执行一次扫描，之后按照 `scanner.interval_seconds` 周期持续扫描。管理界面可以编辑 GitHub / GitLab 连接、仓库、全局与分层环境变量、Agent、sub-agent 白名单和 MR/PR 触发规则，也可以立即扫描、暂停调度并查看 Agent 日志和父子运行记录。

`serve` 本身是前台常驻进程。生产环境请交给 systemd、launchd 或容器平台保活，不要使用应用内 fork。可直接参考：

- [systemd 服务模板](deploy/teamwork-review-agents.service)
- [macOS launchd 模板](deploy/com.teamwork.review-agents.plist)

### `scan-once`：扫描一次后退出

```bash
teamwork-review-agents scan-once
```

执行一个完整周期：扫描全部启用仓库、保存 MR/PR 快照、生成状态变化事件、匹配触发规则并等待对应 Agent 执行完成，最后将本次统计结果输出为 JSON 后退出。适合手动调试、CI 或由 cron 调用。

### `scan-once --dry-run`：只扫描，不启动 Agent

```bash
teamwork-review-agents scan-once --dry-run
```

该模式仍会请求 Provider、保存最新快照并生成事件，但不会处理事件或启动 Agent。生成的事件会保留为待处理状态，之后执行普通 `scan-once` 或启动 `serve` 时仍可能被处理。

首次看到一个 MR/PR 时，系统默认只建立快照基线，不产生触发事件。将 `scanner.emit_initial_events` 改为 `true` 后，首次发现会生成 `change_request.discovered` 事件，并按照规则决定是否启动 Agent。

## GitHub / GitLab 连接与仓库

“GitHub / GitLab 连接”是后台扫描远端 MR / PR 时使用的平台 API 配置，不是 Git clone 或 SSH 地址。连接中只保存 Token 的宿主机环境变量名，例如 `GITHUB_TOKEN`；真实 Token 由启动服务的系统环境提供，不会写入 `config.yaml`。

仓库配置同时关联两类位置：

- 远端项目路径，例如 GitHub 的 `owner/repository` 或 GitLab 的 `group/project`，用于平台 API 扫描。
- 本地 Git 工作目录，用于 Codex CLI 读取或修改代码；服务不会根据平台连接自动克隆仓库，需要提前准备该目录。

推荐在管理 UI 中按“添加平台连接 → 添加仓库 → 启用仓库 → 保存配置”的顺序操作。

## Agent 与 sub-agent

触发规则中的 Agent 是本次事件直接启动的根 Agent。“允许调用的 sub-agent”只是白名单权限：根 Agent 可以在确有必要时通过 `invoke_agent` 委托其中一个 Agent，但勾选后不会自动执行，也不会改变触发规则。

每次 sub-agent 调用都会启动独立的 Codex CLI 进程，拥有自己的运行 ID、超时和日志，并继承当前仓库与 MR/PR 上下文。当前 Agent 不能调用自身；如果没有候选项，需要先创建另一个 Agent。

Agent 的 Prompt 支持两种来源：

- 内联模板：直接在管理 UI 中编辑。
- 文件：手工填写相对于 `config.yaml` 的路径，选择 `./prompts/` 中已有文件，或点击“从电脑选择并导入”。

从电脑导入时，后台会将 UTF-8 编码的 `.md` 或 `.txt` 文件复制到配置目录旁的 `./prompts/`，然后自动填写相对路径。浏览器原始路径不会被写入配置；同名但内容不同的文件会自动增加数字后缀。

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
