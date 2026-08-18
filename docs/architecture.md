# Teamwork Review Agents 当前架构

![当前架构与 Agent 流程](assets/teamwork-review-agents-architecture.png)

## 1. 一句话总览

系统通过周期性扫描 GitHub PR / GitLab MR，把远端状态变化转换为可幂等处理的语义事件，再按 YAML 规则选择 Codex Agent；每次运行在独立 Git worktree 中执行，并由 SQLite 统一保存快照、事件、锁、运行状态和流式日志。

## 2. 系统分层

1. **管理与入口层**：CLI 负责启动、停止、单次扫描和配置校验；React 管理界面通过 FastAPI 查看状态、编辑配置、手动扫描、暂停调度、取消运行和读取实时日志。
2. **配置与运行层**：`ConfigManager` 负责 YAML 校验、版本和热加载；`BackgroundRuntime` 在 FastAPI 生命周期内串行发起扫描周期。
3. **采集与事件层**：GitHub / GitLab Provider 将平台数据归一为 `ChangeRequestSnapshot`；事件检测器比较快照及活动时间线，生成 `opened`、`merged`、`commits_changed` 等语义事件。
4. **规则与编排层**：`Orchestrator` 从 SQLite 领取待处理事件，以规则的事件名、仓库和字段条件进行匹配；同一轮的多个事件可以按 `deduplicate_per_scan` 合并为一次 Agent 运行。
5. **Agent 执行层**：`AgentExecutor` 负责幂等预约、写资源锁、环境变量与 Prompt 组装、Secret 脱敏、临时 worktree 和清理策略；`CodexRunner` 启动 `codex exec --json --ephemeral` 并持续保存 JSONL 日志。
6. **委托层**：根 Agent 只能通过应用注入的 MCP `invoke_agent` 调用白名单中的 sub-agent；系统限制递归深度、根任务总运行数和调用环。
7. **持久化层**：SQLite 是跨进程一致性的中心，保存快照、活动游标、事件收件箱、事件到 Agent 的调度关系、Agent 运行、日志、配置版本、服务状态和资源租约。

## 3. 内置 Agent、主要思路与触发时机

仓库当前没有实际 `config.yaml`，所以下表描述的是 `config_example.yaml` 随附的三个内置 Agent 模板。两条内置规则都设置为 `enabled: false`，管理员启用后才会自动执行。

| Agent | 做什么 | 一句话主要思路 | 触发时机 |
| --- | --- | --- | --- |
| `general-reviewer` | 对 GitHub PR / GitLab MR 做完整代码审核，读取项目审核 Skill、设计文档和历史变更，校验源/目标 SHA、CI、可合并状态与平台门禁；最终发布一条顶层审核评论，只有全部通过才自动合并。 | **固定一对源/目标 SHA 作为不可变审核基准，以代码证据和平台门禁共同决定是否合并。** | 内置规则 `general-review` 在 `change_request.opened` 或 `change_request.reopened` 时直接触发；同轮事件对该 Agent 去重。标题精确为 `dependency(auto-update)`、`doc(auto-update)` 或 `auto-update` 时，Agent 自身会跳过。 |
| `incremental-doc-update-runner` | 编排已合并 GitHub PR / GitLab MR 的文档自动更新：验证原始变更请求和合并前后 SHA，创建独立文档分支，调用文档子 Agent，核验其提交拓扑与远程状态，在同一平台创建 `doc(auto-update)` PR / MR，等待平台门禁、自动合并并清理分支。它不亲自判断或修改文档。 | **主 Agent 只管边界、分支、门禁与清理，把文档判断交给专职子 Agent。** | 内置规则 `增量文档更新` 在 `change_request.merged` 时直接触发，并设置 `inherit_workspace: true`。Prompt 根据仓库上下文中的 `provider_kind` 选择 GitHub 或 GitLab 路径；自动更新标题会被防循环逻辑跳过。 |
| `incremental-doc-updater` | 在 Runner 指定的固定提交范围内，通过文档索引建立最小候选集，只更新确实受影响的文档和索引；有修改时仅创建一个题为 `doc(auto-update)` 的提交并推送，返回结构化状态。它不创建或合并 PR / MR。 | **用固定净差异和文档索引做增量路由，以最少读取完成最小必要更新。** | 没有内置规则直接触发；仅当 Runner 完成 PR / MR、SHA、分支和环境变量校验后，通过 MCP `invoke_agent` 委托。默认 Agent 名来自 `INCREMENTAL_DOC_UPDATE_AGENT_NAME=incremental-doc-updater`。 |

## 4. 关键运行流程

1. 服务启动后立即扫描，之后按 `scanner.interval_seconds` 周期执行；管理 UI 也可以请求立即扫描。
2. Provider 拉取启用仓库的 PR / MR 和可用的活动时间线，事件检测器把新旧快照差异写入 SQLite 事件收件箱。
3. 编排器领取待处理事件，按规则规划 Agent 调用；未匹配事件标记为未触发，匹配事件记录事件到 Agent 的调度关系。
4. 执行器为运行创建幂等记录并申请资源租约。声明 `workspace` 写操作时，同一源分支串行；声明 `change_request` 写操作时，同一 PR / MR 串行。
5. 根 Agent 默认从当前变更请求 Head 创建独立 detached worktree。文档 Runner 的规则开启工作区继承，因此它调用的 Updater 可复用同一 worktree、分支和未提交状态，委托期间串行执行。
6. Codex CLI 只接收白名单环境变量；Provider Token 被强制从 Prompt 和 Codex 子进程剥离。应用临时注入 Agent 选择的 Skill 和仅含 `invoke_agent` 的 MCP 网关。
7. stdout JSONL、stderr、最终消息、用量、超时、取消与工作区清理状态持续写入 SQLite，并通过 FastAPI SSE 提供给 React UI。

## 5. 两条核心业务链

### 代码审核链

`opened / reopened` → `general-reviewer` → 固定 SHA → 等待 CI 与平台门禁 → 完整审核 → 发布总结评论 → 全部通过才合并。

### 合并后文档链

`merged` → `incremental-doc-update-runner` → 验证合并边界并创建文档分支 → MCP 调用 `incremental-doc-updater` → 更新文档并推送唯一提交 → Runner 核验 → 在原平台创建文档 PR / MR → 门禁通过后合并并清理。

## 6. 当前实现的关键边界

- 触发来自轮询而非 Webhook，因此延迟由扫描周期决定；GitHub Timeline 可恢复部分轮询间离散动作，GitLab 主要依赖快照差异。
- `write_scopes` 是编排器的锁与审计声明，不是平台权限或操作系统强隔离；真实权限仍取决于部署账号、Sandbox 和外部工具。
- sub-agent 白名单只授予“可以调用”的能力，不代表每次根 Agent 都会委托。
- 文档更新 Runner 只以仓库上下文中的 `provider_kind` 选择平台路径：GitHub 核验 Checks、Reviews、Branch Protection / Ruleset，GitLab 核验 Pipeline、Approval 和保护分支；字段缺失或平台冲突时安全停止。
- 内置规则默认关闭；未启用时系统仍会扫描并记录快照与事件，但不会自动运行这三个 Agent。
