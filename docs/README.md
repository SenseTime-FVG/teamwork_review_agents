# 文档索引

本索引用于导航仓库中由维护者阅读的文档，并为增量文档更新提供路由。内置 Agent Prompt、前端构建产物和图片资源不属于本索引。

## 入门、运行与维护

| 文档 | 用途 | 关联代码/模块 | 更新触发条件 |
| --- | --- | --- | --- |
| [`README.md`](../README.md) | 项目入口、安装、核心能力和常用配置 | CLI、运行时、管理界面 | 首次使用流程、公开命令、主要能力或安全边界变化 |
| [`CONTRIBUTING.md`](../CONTRIBUTING.md) | 本地开发、验证和贡献约定 | 开发依赖、测试、前端构建 | 开发命令、验证要求或目录职责变化 |
| [`first-time-setup.md`](first-time-setup.md) | 管理界面首次配置图文流程 | 平台连接、仓库、Agent 与规则配置 | 首次配置界面、步骤或截图变化 |
| [`platform-cli-auth.md`](platform-cli-auth.md) | `gh` / `glab` 本机登录配置 | 平台 CLI 集成 | 登录前提、命令或所需权限变化 |
| [`operations.md`](operations.md) | 部署、权限、启停与排障 | 服务进程、运行数据、认证和隔离 | 部署方式、运行生命周期、凭据边界或故障处理变化 |
| [`preflight-ci.md`](preflight-ci.md) | GitHub / GitLab Preflight CI 门禁语义 | Preflight 执行器、事件和状态回写 | CI 配置、执行、幂等或安全边界变化 |
| [`architecture.md`](architecture.md) | 当前系统分层、主链路与边界 | Provider、事件、执行器、SQLite 与内置 Agent | 架构、数据流、Agent 职责或平台支持变化 |

## 当前设计

| 文档 | 用途 | 关联代码/模块 | 更新触发条件 |
| --- | --- | --- | --- |
| [`design-codex-model-catalog-refresh.md`](design-codex-model-catalog-refresh.md) | 当前 CLI 的模型目录刷新与缓存回退 | App Server、运行时接口、Provider UI | 目录来源、刷新与降级语义变化；实施见 [`implementation-plan-codex-model-catalog-refresh.md`](implementation-plan-codex-model-catalog-refresh.md) |
| [`design-effort-inheritance-display.md`](design-effort-inheritance-display.md) | effort 继承值和来源展示 | Codex 诊断、全局默认与两级回退链 UI | 默认值、模式和展示语义变化；实施见 [`implementation-plan-effort-inheritance-display.md`](implementation-plan-effort-inheritance-display.md) |
| [`design-gitlab-activities.md`](design-gitlab-activities.md) | GitLab 结构化活动、游标与手动重放来源 | Provider、事件、SQLite、Web 与概览 | 活动读取、降级、去重和手动触发行为变化；实施见 [`implementation-plan-gitlab-activities.md`](implementation-plan-gitlab-activities.md) |
| [`design-gitlab-cli-host.md`](design-gitlab-cli-host.md) | 自建 GitLab CLI 默认主机与临时 HOME 认证路由 | `environment.py`、Agent 与仓库进程 | CLI 主机默认值、分层覆盖或凭据传递变化；实施见 [`implementation-plan-gitlab-cli-host.md`](implementation-plan-gitlab-cli-host.md) |
| [`design.md`](design.md) | 当前系统实现语义的权威设计记录 | `src/teamwork_review_agents/`、`ui/src/` | 已实现的配置、事件、运行时、API 或 UI 语义变化 |
| [`design-quick-setup.md`](design-quick-setup.md) | 一键配置、规则范围和仓库级 Skill 隔离 | `quick_setup.py`、`QuickSetupWizard.tsx` | 向导步骤、规则适用性、凭据或保存语义变化 |
| [`design-git-provider-credential-context.md`](design-git-provider-credential-context.md) | 原生 Git 临时凭据程序与错误脱敏 | `git_auth.py`、`workspace.py`、`sandbox_git.py` | Git 凭据传递、跨平台入口或故障展示变化 |
| [`design-background-startup-confirmation.md`](design-background-startup-confirmation.md) | 后台启动等待与健康检查诊断 | `process_manager.py`、`cli.py` | 启动时限、健康探测或启动确认语义变化 |
| [`design-model-runtime-log-normalization.md`](design-model-runtime-log-normalization.md) | 模型基座运行日志语义设计 | 模型基座运行时与日志 | 模型基座日志事件或展示语义变化 |
| [`design-agent-workspace-preparation.md`](design-agent-workspace-preparation.md) | Agent 工作区准备与仓库级依赖缓存设计 | `agent_workspace.py`、缓存与仓库配置 | `agent_workspace`、准备步骤、缓存、沙盒或相关 UI 变化 |
| [`design-agent-workspace-snapshot-warmup.md`](design-agent-workspace-snapshot-warmup.md) | 工作区依赖快照与手动预热设计 | `workspace_snapshot.py`、工作区预热 API 与 UI | 快照指纹、归档恢复、容量策略、预热流程或状态变化 |
| [`design-managed-comment-model-signature.md`](design-managed-comment-model-signature.md) | 托管评论模型签名设计 | `managed_comments.py`、Agent 配置与评论 UI | 签名开关、模型快照来源、评论格式或生命周期变化 |
| [`design-managed-skill-authoring.md`](design-managed-skill-authoring.md) | 受管 Skill 在线新建与编辑设计 | `skill_files.py`、Skill 管理 API 与 UI | Skill 在线创建、编辑、元数据或受管目录边界变化 |
| [`design-repository-skill-policy.md`](design-repository-skill-policy.md) | 仓库级 Skill 白名单设计 | 仓库配置、Agent Skill 交集与仓库详情 UI | `allowed_skills` 或运行时 Skill 选择语义变化 |
| [`design-combined-update-dependency-failure-continuation.md`](design-combined-update-dependency-failure-continuation.md) | 组合更新中依赖失败后的文档续跑设计 | 组合更新 Runner Prompt 与两个子 Agent 的阶段契约 | 依赖失败恢复、文档续跑或部分完成语义变化 |
| [`design-context-fork-compaction.md`](design-context-fork-compaction.md) | 原对话副本摘要与 90% 默认阈值设计 | 上下文管理、模型运行器与协议适配 | 压缩请求结构、预算或隔离语义变化 |
| [`design-review-comment-isolation.md`](design-review-comment-isolation.md) | 通用审核的评论读取隔离设计 | 通用审核 Prompt | 描述、评论正文和平台门禁的读取边界变化 |

## 实施记录

以下文档记录对应阶段的实施方案和验收范围；新增实现不应改写其中已经完成的历史结论。

| 文档 | 用途 | 关联代码/模块 | 更新触发条件 |
| --- | --- | --- | --- |
| [`implementation-plan.md`](implementation-plan.md) | 项目阶段实施与验收记录 | 全项目历史实施范围 | 新增需要记录的实施阶段或验收结论 |
| [`implementation-plan-quick-setup.md`](implementation-plan-quick-setup.md) | 一键配置实施与验收 | 向导、配置管理及测试 | 实施范围或验证结果变化 |
| [`implementation-plan-git-provider-credential-context.md`](implementation-plan-git-provider-credential-context.md) | Git 凭据上下文和启动入口修正记录 | 原生 Git、沙盒与向导检查 | 凭据实现、生命周期或实际 Git 验证结果变化 |
| [`implementation-plan-background-startup-confirmation.md`](implementation-plan-background-startup-confirmation.md) | 后台启动确认实施与验收范围 | 进程管理与 CLI 测试 | 该实施方案或验收记录更新 |
| [`implementation-plan-model-runtime-log-normalization.md`](implementation-plan-model-runtime-log-normalization.md) | 模型基座运行日志语义实施方案 | 模型基座运行时与日志 | 该实施方案本身或其验收记录更新 |
| [`implementation-plan-agent-workspace-preparation.md`](implementation-plan-agent-workspace-preparation.md) | 工作区准备与依赖缓存实施方案 | 工作区准备、缓存和管理界面 | 该实施方案或验收记录更新 |
| [`implementation-plan-agent-workspace-snapshot-warmup.md`](implementation-plan-agent-workspace-snapshot-warmup.md) | 工作区依赖快照与预热实施方案 | 快照、预热 API 和管理界面 | 该实施方案或验收记录更新 |
| [`implementation-plan-managed-comment-model-signature.md`](implementation-plan-managed-comment-model-signature.md) | 托管评论模型签名实施方案 | 托管评论、模型快照和 Agent 配置 | 该实施方案或验收记录更新 |
| [`implementation-plan-managed-skill-authoring.md`](implementation-plan-managed-skill-authoring.md) | 受管 Skill 在线新建与编辑实施方案 | Skill 文件管理、API 和配置界面 | 该实施方案或验收记录更新 |
| [`implementation-plan-repository-skill-policy.md`](implementation-plan-repository-skill-policy.md) | 仓库级 Skill 白名单实施方案 | 配置校验、运行器和仓库详情 UI | 该实施方案或验收记录更新 |
| [`implementation-plan-combined-update-dependency-failure-continuation.md`](implementation-plan-combined-update-dependency-failure-continuation.md) | 组合更新中依赖失败后的文档续跑实施方案 | 组合更新 Runner Prompt 与协议测试 | 该实施方案或验收记录更新 |
| [`implementation-plan-context-fork-compaction.md`](implementation-plan-context-fork-compaction.md) | 原对话副本压缩实施与回归范围 | 上下文管理与模型协议测试 | 该实施方案或验收记录更新 |
| [`implementation-plan-review-comment-isolation.md`](implementation-plan-review-comment-isolation.md) | 通用审核的评论读取隔离实施方案 | 通用审核 Prompt 与渲染测试 | 该实施方案或验收记录更新 |
