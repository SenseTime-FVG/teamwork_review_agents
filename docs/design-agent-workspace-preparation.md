# Agent 工作区准备与仓库级依赖缓存设计

## 背景

Agent 运行使用独立的临时 Git 工作区，基础仓库或其他运行中已经存在的 `node_modules`、虚拟环境等安装结果不会被复制进来。即使本地 Preflight / CI 已经拥有下载缓存，Agent 自主执行 `npm run build` 等命令时仍可能因为工作区没有安装依赖而失败。

本功能不修改 Agent Prompt，也不推断仓库使用 npm、uv 或其他包管理器。仓库管理员显式配置模型启动前需要执行的准备步骤，Teamwork 只负责在当前 Agent 工作区中按顺序执行这些参数数组命令，并为它们和后续 Agent 命令注入同一个仓库级下载缓存环境。

## 配置模型

每个仓库新增 `agent_workspace` 配置：

```yaml
agent_workspace:
  cache_enabled: true
  timeout_seconds: 1800
  max_output_bytes: 1000000
  prepare_steps:
    - name: 安装前端依赖
      cwd: ui
      command:
        - npm
        - ci
      timeout_seconds: 900
```

- `cache_enabled`：是否为该仓库的准备步骤及 Agent 命令注入下载缓存环境。
- `timeout_seconds`：全部准备步骤的总超时。
- `max_output_bytes`：运行记录中保留的准备输出上限。
- `prepare_steps`：按配置顺序运行；空列表表示不执行准备命令。
- `cwd`：相对当前 Agent 工作区的目录，默认 `.`；禁止绝对路径、`..` 和符号链接逃逸。
- `command`：直接传给进程执行器的参数数组，第一个元素是可执行程序；不经过 Shell。
- `timeout_seconds`：可选单步超时，同时受总超时约束。

复杂安装流程由仓库提供脚本，配置只调用该脚本，避免在管理页面实现 Shell 语法。

## 缓存边界

缓存按稳定仓库身份分目录，不按分支或 MR / PR 分目录。同一仓库的不同分支共享下载缓存，不同仓库永不共享。现有 Preflight 缓存路径保持不变，以兼容已经下载的数据；新增通用变量 `TEAMWORK_REPOSITORY_CACHE_DIR`，并继续保留 `TEAMWORK_PREFLIGHT_CACHE_DIR`。

缓存覆盖已有的 uv、pip、Poetry、PDM、npm、pnpm、Yarn、Bun、Cargo、Go、Maven、Gradle、NuGet、Composer、Playwright、Puppeteer 和 Deno 目录。缓存只保存包管理器下载数据；`node_modules`、虚拟环境和构建产物仍位于每次运行的隔离工作区。

共享缓存可能被同一仓库中的不可信变更影响，因此只允许在同一仓库身份内复用。依赖完整性仍由锁文件和包管理器校验负责。

## 执行顺序与沙盒

运行顺序为：

1. 创建或继承本次 Agent 工作区。
2. 解析 Agent 环境并准备仓库级缓存目录。
3. 在 Teamwork 外层沙盒中执行 `prepare_steps`。
4. 准备成功后记录 `workspace.prepared`，再启动模型或 Codex CLI。

准备命令使用 Agent 的联网策略，但文件权限固定为工作区可写，并额外只放行当前仓库的缓存根目录。受限 Agent 必须能够启用 Teamwork 外层沙盒；`danger-full-access` Agent 沿用其显式的完全访问配置。这样不会为了安装依赖扩大模型本身的工作区权限。

缓存环境同时注入后续 Agent 进程，因此模型再次调用包管理器时仍可命中相同下载缓存。缓存变量不写入 Prompt，也不包含 Provider 或模型凭据。

## 日志与失败行为

准备阶段写入当前 Agent 运行日志：

- `workspace.prepare.started`
- `workspace.prepare.step_started`
- `workspace.prepare.output`
- `workspace.prepare.step_completed`
- `workspace.prepare.completed`
- `workspace.prepare.failed`

输出受字节上限约束并经过现有 Secret 脱敏。任一步骤退出非零、超时、无法启动或被取消时，Agent 不启动模型；运行进入失败、超时或取消状态，并保留失败步骤和有界输出。

## 前端交互

仓库详情增加“Agent 工作区准备”区块：

- 仓库级下载缓存开关；
- 准备总超时和最大日志字节数；
- 可排序的准备步骤；
- 每步展示名称、相对目录、执行程序、单步超时和逐行参数。

帮助文案明确说明命令由用户定义、直接按参数数组执行，以及 `cwd: ui` + `npm` + `ci` 的等价命令是 `cd ui && npm ci`，但实际不会启动 Shell。
