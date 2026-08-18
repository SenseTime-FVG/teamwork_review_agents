# Teamwork Review Agents

> Vibe coding 负责快速产出代码；本项目负责让代码可靠进入主分支。

PR / MR 越来越快、越来越多，但审核、CI、合并门禁和文档同步仍靠人工。结果是审核缺少上下文、旧结论误用于新提交、合并后文档过期。

Teamwork Review Agents 持续扫描 GitHub PR / GitLab MR，把状态变化转换为事件，再按规则启动隔离的 Codex Agent，完成审核、合并和文档更新。

## 方案

- 固定源、目标 SHA，结合完整 diff、设计、历史变更和 CI 审核。
- 每次 Agent 使用独立 Git worktree，避免并发任务互相污染。
- 合并后按净差异和文档索引，只更新受影响文档。
- 快照、事件、规则、运行和日志统一写入 SQLite，并在管理界面展示。

主链路：`扫描 PR/MR → 语义事件 → 规则匹配 → Codex Agent → 审计与日志`

![Teamwork Review Agents 的当前架构与 Agent 流程](docs/assets/teamwork-review-agents-architecture.png)

| Agent | 职责 | 触发时机 |
| --- | --- | --- |
| `general-reviewer` | 审核代码和门禁；全部通过才评论并合并 | PR / MR `opened`、`reopened` |
| `incremental-doc-update-runner` | 编排文档分支、sub-agent、文档 MR 和清理 | GitLab MR `merged` |
| `incremental-doc-updater` | 增量更新受影响文档 | Runner 通过 MCP 委托 |

内置规则默认关闭。先验证扫描，再按需启用。

## 快速开始

运行端需要 Linux / macOS、Python 3.11+、Git、已登录的 Codex CLI，以及 GitHub / GitLab Token。Agent 需要评论、推送或合并时，还需为同一系统用户配置 `gh` / `glab` 登录。

```bash
python -m pip install -e .
cp config_example.yaml config.yaml
teamwork-review-agents validate
teamwork-review-agents start
```

打开 [http://127.0.0.1:8080](http://127.0.0.1:8080)，点击“编辑配置”：

1. 添加 GitHub / GitLab 连接，填写 API 地址和 Token 变量名。
2. 添加目标仓库，填写远端地址和本地基础仓库目录并启用；目录不存在时会自动克隆。
3. 在全局环境中引用宿主机的 `GITHUB_TOKEN` 或 `GITLAB_TOKEN`。
4. 检查 Codex、Agent 权限和 Prompt，保存后执行“立即扫描”。
5. 确认仓库、事件和日志正常，再启用触发规则。

Provider Token 只供扫描器使用，不会传给 Codex。平台写操作使用独立的最小权限 `gh` / `glab` 身份。

## 目标仓库准备

扫描本身不要求目标仓库新增文件。为提高审核质量并避免文档 Agent 首次全量扫描，建议提交：

```text
your-project/
├── AGENTS.md
└── docs/
    ├── README.md
    ├── design/
    │   └── README.md
    └── changes/
        └── README.md
```

其中 `docs/design/` 和 `docs/changes/` 可选；没有对应文档时不要创建空目录。

### `AGENTS.md`

记录 Agent 必须遵守的项目命令和边界：

```markdown
# Project rules

- 安装：`<实际命令>`
- 测试：`<实际命令>`
- 静态检查：`<实际命令>`
- 主要代码：`<路径>`
- 禁止修改：`<生成目录或受保护路径>`
- 公开接口、配置或行为变化时，同步更新 `docs/`。
```

提交前替换全部占位符。

### `docs/README.md`

这是文档 Agent 默认使用的索引：

```markdown
# 文档索引

| 文档 | 用途 | 关联模块 | 更新条件 |
| --- | --- | --- | --- |
| `README.md` | 项目入口 | CLI、启动配置 | 命令或首次使用流程变化 |
| `docs/api.md` | API 说明 | `src/api/` | 路由、鉴权或请求响应变化 |
```

若该文件不存在，第一次文档任务会扫描全部非排除文档并自动创建索引，耗时和 Token 消耗更高。

### 设计与变更历史

- `docs/design/`：保存仍有效的架构、ADR 和 Spec。
- `docs/changes/`：保存重要变更、迁移、回滚和兼容性决策。

配置固定目录后，路径必须已存在且可读；否则留空，让审核 Agent 自动发现。

在目标仓库环境中配置：

| 变量 | 示例值 |
| --- | --- |
| `REVIEW_DESIGN_DOC_DIR` | `docs/design` |
| `REVIEW_CHANGE_HISTORY_DIR` | `docs/changes` |
| `DOC_UPDATE_INDEX_PATH` | `docs/README.md` |
| `DOC_UPDATE_EXCLUDE_DIRECTORIES` | `vendor,generated` |

这些变量需要开启“传给进程”。`REVIEW_SKILLS` 不是仓库路径；Skill 应导入本服务后再分配给 Agent。

## 首次启用

1. 保持规则关闭，执行一次“立即扫描”。
2. 启用 `general-review`，用新的 PR / MR 验证审核链。
3. 准备好 `docs/README.md` 后，再启用“增量文档更新”。

当前内置文档 Runner 面向 GitLab MR；通用审核同时支持 GitHub PR 和 GitLab MR。

## 常用命令

```bash
teamwork-review-agents validate
teamwork-review-agents start
teamwork-review-agents restart
teamwork-review-agents stop
teamwork-review-agents run
teamwork-review-agents scan-once
teamwork-review-agents scan-once --dry-run
teamwork-review-agents runs --limit 20
```

默认读取当前目录的 `config.yaml`。其他配置文件使用 `-c /path/to/config.yaml`。

## 文档导航

| 文档 | 用途 |
| --- | --- |
| [`config_example.yaml`](config_example.yaml) | 完整配置字段和默认值 |
| [`docs/operations.md`](docs/operations.md) | 部署、权限、启停和排障 |
| [`docs/architecture.md`](docs/architecture.md) | 架构、Agent 边界和数据流 |
| [`docs/design.md`](docs/design.md) | 精确实现语义 |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 本地开发、测试和前端联调 |
| [`docs/implementation-plan.md`](docs/implementation-plan.md) | 历史实施阶段和验收记录 |
| [`deploy/`](deploy/) | systemd / launchd 模板 |

管理界面默认只监听 `127.0.0.1`。对外监听时必须配置 `web.admin_token_env`。
