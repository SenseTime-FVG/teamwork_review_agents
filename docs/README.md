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
| [`preflight-ci.md`](preflight-ci.md) | GitHub Preflight CI 门禁语义 | Preflight 执行器、事件和状态回写 | CI 配置、执行、幂等或安全边界变化 |
| [`architecture.md`](architecture.md) | 当前系统分层、主链路与边界 | Provider、事件、执行器、SQLite 与内置 Agent | 架构、数据流、Agent 职责或平台支持变化 |

## 当前设计

| 文档 | 用途 | 关联代码/模块 | 更新触发条件 |
| --- | --- | --- | --- |
| [`design.md`](design.md) | 当前系统实现语义的权威设计记录 | `src/teamwork_review_agents/`、`ui/src/` | 已实现的配置、事件、运行时、API 或 UI 语义变化 |
| [`design-model-runtime-log-normalization.md`](design-model-runtime-log-normalization.md) | 模型基座运行日志语义设计 | 模型基座运行时与日志 | 模型基座日志事件或展示语义变化 |
| [`design-agent-workspace-preparation.md`](design-agent-workspace-preparation.md) | Agent 工作区准备与仓库级依赖缓存设计 | `agent_workspace.py`、缓存与仓库配置 | `agent_workspace`、准备步骤、缓存、沙盒或相关 UI 变化 |
| [`design-agent-workspace-snapshot-warmup.md`](design-agent-workspace-snapshot-warmup.md) | 工作区依赖快照与手动预热设计 | `workspace_snapshot.py`、工作区预热 API 与 UI | 快照指纹、归档恢复、容量策略、预热流程或状态变化 |
| [`design-managed-comment-model-signature.md`](design-managed-comment-model-signature.md) | 托管评论模型签名设计 | `managed_comments.py`、Agent 配置与评论 UI | 签名开关、模型快照来源、评论格式或生命周期变化 |

## 实施记录

以下文档记录对应阶段的实施方案和验收范围；新增实现不应改写其中已经完成的历史结论。

| 文档 | 用途 | 关联代码/模块 | 更新触发条件 |
| --- | --- | --- | --- |
| [`implementation-plan.md`](implementation-plan.md) | 项目阶段实施与验收记录 | 全项目历史实施范围 | 新增需要记录的实施阶段或验收结论 |
| [`implementation-plan-model-runtime-log-normalization.md`](implementation-plan-model-runtime-log-normalization.md) | 模型基座运行日志语义实施方案 | 模型基座运行时与日志 | 该实施方案本身或其验收记录更新 |
| [`implementation-plan-agent-workspace-preparation.md`](implementation-plan-agent-workspace-preparation.md) | 工作区准备与依赖缓存实施方案 | 工作区准备、缓存和管理界面 | 该实施方案或验收记录更新 |
| [`implementation-plan-agent-workspace-snapshot-warmup.md`](implementation-plan-agent-workspace-snapshot-warmup.md) | 工作区依赖快照与预热实施方案 | 快照、预热 API 和管理界面 | 该实施方案或验收记录更新 |
| [`implementation-plan-managed-comment-model-signature.md`](implementation-plan-managed-comment-model-signature.md) | 托管评论模型签名实施方案 | 托管评论、模型快照和 Agent 配置 | 该实施方案或验收记录更新 |
