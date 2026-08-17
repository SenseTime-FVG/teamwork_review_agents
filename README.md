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
teamwork-review-agents start
```

所有命令默认读取当前工作目录的 `config.yaml`。需要使用其他配置时，仍可通过 `-c /path/to/other.yaml` 覆盖。

全部配置项、默认值和字段用途见 [`config_example.yaml`](config_example.yaml)。其中的业务示例保持注释状态，复制后不会在首次 UI 中创建示例仓库、Agent 或规则。

首次启动时，管理 UI 中的 GitHub / GitLab 连接、仓库、环境变量、Agent 和触发规则都是空的。访问 [http://127.0.0.1:8080](http://127.0.0.1:8080) 后按实际需要添加配置并保存即可。

## 运行模式

### `start`：在后台启动

```bash
teamwork-review-agents start
```

校验配置后启动脱离当前终端的 FastAPI 管理服务和后台调度器。命令会输出 PID、管理界面地址和后台日志路径；关闭当前终端不会停止服务。服务启动后立即执行一次扫描，之后默认每 5 分钟持续扫描；底层配置仍以 `scanner.interval_seconds: 300` 保存秒数。

默认运行文件位于 `config.yaml` 同目录的 `data/`：

- `teamwork-review-agents.pid`：当前进程身份。
- `teamwork-review-agents.lock`：防止同一配置重复启动。
- `teamwork-review-agents.log`：后台进程的标准输出与错误日志。

Agent 运行日志仍保存在 SQLite 中，并可以在管理 UI 查看。

### `run`：在前台运行

```bash
teamwork-review-agents run
```

功能与 `start` 相同，但进程保留在当前终端，按 `Ctrl+C` 结束。`serve` 作为旧版本兼容命令保留，行为等同于 `run`。

### `stop` / `end`：结束服务

```bash
teamwork-review-agents stop
# 或
teamwork-review-agents end
```

两个命令完全等价。默认发送 `SIGTERM` 等待当前服务收尾；30 秒后仍未退出时会强制结束。服务没有运行时命令也会正常返回。

### `restart`：重启后台服务

```bash
teamwork-review-agents restart
```

先校验新配置，再停止当前服务并重新在后台启动。如果服务原本没有运行，则直接启动。

以上命令都默认使用当前目录的 `config.yaml`。自定义配置时必须在启动、停止和重启时传入同一路径：

```bash
teamwork-review-agents start -c /path/to/config.yaml
teamwork-review-agents restart -c /path/to/config.yaml
teamwork-review-agents stop -c /path/to/config.yaml
```

生产环境需要故障自动拉起和开机启动时，仍建议让进程管理器运行前台命令 `run`：

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

该模式仍会请求 Provider、保存最新快照并生成事件，但不会处理事件或启动 Agent。生成的事件会保留为待处理状态，之后执行普通 `scan-once` 或启动 `run` / `start` 时仍可能被处理。

首次看到一个 MR/PR 时，系统默认只建立快照基线，不产生触发事件。将 `scanner.emit_initial_events` 改为 `true` 后，首次发现会生成 `change_request.discovered` 事件，并按照规则决定是否启动 Agent。

管理界面的“已扫描 MR / PR”统计和列表来自已保存的最新快照；“变化事件”是新发现、提交、状态、标签等变化产生的事件，两者不会混为一个计数。对于已经建立基线但没有首次事件的 MR / PR，可以在列表中选择“补发首次事件”。补发不会删除快照或重新请求 Provider，只会幂等写入 `change_request.discovered`，并按当前规则调度；操作前请先确认规则与 Agent 权限。

## GitHub / GitLab 连接与仓库

“GitHub / GitLab 连接”是后台扫描远端 MR / PR 时使用的平台 API 配置，不是 Git clone 或 SSH 地址。连接中的 `token_env` 保存 Provider Token 的变量名，例如 `GITHUB_TOKEN`。系统按以下顺序取值：

1. “全局环境”中的同名变量，可以配置固定值，也可以通过 `from_system` 引用另一个宿主机变量。
2. 如果全局环境没有该变量，直接读取启动服务时的同名宿主机环境变量，以兼容原有配置。

推荐在“全局环境”中选择“宿主机环境”来源，例如让 `GITHUB_TOKEN` 读取宿主机的 `GITHUB_TOKEN`，这样真实 Token 不会写入 `config.yaml`。如果选择“固定值”，Token 会以明文保存在本机且已被 Git 忽略的 `config.yaml` 中，但管理 API、UI 和配置历史只会显示 `********`。

与 Provider `token_env` 同名的环境变量会自动标记为 Provider 凭据：系统强制将其视为 Secret，并禁止进入 Prompt 和 Codex 子进程。它只供后台扫描器访问平台 API。

仓库配置同时关联两类位置：

- 远端仓库地址或项目路径，用于平台 API 扫描。支持 GitHub 的 `owner/repository`、GitLab 的 `group/project`、`git@host:group/project.git` SSH 地址和 HTTPS Git 地址；后台会自动提取规范项目路径。
- 本地 Git 工作目录，用于 Codex CLI 读取或修改代码。目录不存在时，服务会根据 SSH/HTTPS 地址自动克隆；目录已经存在时只校验并更新远端引用，不会覆盖文件。

推荐在管理 UI 中按“添加平台连接 → 添加仓库 → 启用仓库 → 保存配置”的顺序操作。

每次 Agent 启动前，服务会执行 `git fetch`，并把当前 MR/PR head 保存到 `refs/teamwork/change-requests/<编号>/head`。Prompt 运行上下文会同时提供这个引用和目标分支引用，Agent 可以直接执行 `git diff`。系统不会自动切换本地当前分支，避免破坏用户已有修改；需要写代码的 Agent 仍应按自己的任务边界创建或切换工作分支。

扫描器会按更新时间倒序自动翻页，读取到上次成功扫描时间之前的数据后提前停止。`scanner.max_items_per_repository` 是单个仓库每轮的安全上限，默认 100 条；它限制需要继续读取详情、Review、流水线与合并状态的 MR/PR 数量，避免大型仓库单轮请求失控。`scanner.api_page_size` 只是高级分页参数，通常无需修改。

## Agent 与 sub-agent

触发规则中的 Agent 是本次事件直接启动的根 Agent。“允许调用的 sub-agent”只是白名单权限：根 Agent 可以在确有必要时通过 `invoke_agent` 委托其中一个 Agent，但勾选后不会自动执行，也不会改变触发规则。

每次 sub-agent 调用都会启动独立的 Codex CLI 进程，拥有自己的运行 ID、超时和日志，并继承当前仓库与 MR/PR 上下文。当前 Agent 不能调用自身；如果没有候选项，需要先创建另一个 Agent。

Agent 的 Prompt 支持两种来源：

- 内联模板：直接在管理 UI 中编辑。
- 文件：手工填写相对于 `config.yaml` 的路径，选择 `./prompts/` 中已有文件，或点击“从电脑选择并导入”。

从电脑导入时，后台会将 UTF-8 编码的 `.md` 或 `.txt` 文件复制到配置目录旁的 `./prompts/`，然后自动填写相对路径。浏览器原始路径不会被写入配置；同名但内容不同的文件会自动增加数字后缀。

仓库根目录的 `/prompts/` 默认由 Git 忽略，用于保存每个部署环境自己的 Prompt，不会因为 UI 导入而进入版本控制。项目目前不包含内置 Prompt；以后新增内置 Prompt 时，应只为明确指定的内置文件增加 `.gitignore` 例外，不要解除整个目录的忽略规则。

## 环境变量与 Prompt 模板

配置按“全局 → 仓库 → Agent → 运行时内置变量”合并，后面的同名变量覆盖前面的值：

```yaml
environment:
  global:
    ORGANIZATION_NAME: Example Team
    GITHUB_TOKEN:
      from_system: GITHUB_TOKEN
      secret: true
      expose_to_prompt: false
      expose_to_process: false
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

模板中的未定义变量会渲染为空字符串。Secret 默认不进入 Prompt；配置 API、配置历史、渲染后的 Prompt、Codex JSONL 和 stderr 落库前都会脱敏。UI 中的 `********` 是保留原 Secret 的占位符。Provider 凭据比普通 Secret 更严格：即使手工把 `expose_to_prompt` 或 `expose_to_process` 设为 `true`，运行时也会强制关闭。

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
teamwork-review-agents run
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

- 系统中需要安装 `git`，并准备好克隆地址对应的 SSH Key 或 HTTPS 凭据；工作目录不存在时会自动克隆。
- Codex CLI 已完成认证，或者运行环境提供仅对 `codex exec` 生效的认证方式。
- GitHub 默认使用 `GITHUB_TOKEN`，GitLab 使用连接中 `token_env` 指定的变量；该变量既可在“全局环境”配置，也可直接来自宿主机环境。
- 需要进行 GitHub/GitLab 写操作的 Agent，可以使用本机已认证的 `gh`/`glab`，并应在提示词中明确操作边界。
- Provider Token 永远不会传进 Codex 子进程；写平台操作应使用单独的最小权限身份。

详细架构见 [系统设计](docs/design.md)，分阶段实施与验收项见 [实施方案](docs/implementation-plan.md)。
