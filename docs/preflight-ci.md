# GitHub Preflight CI 门禁

## 职责边界

通用引擎负责准确检出 PR Head、隔离执行、超时与日志控制、结果幂等、GitHub Commit Status 回写，以及成功后启动明确要求 CI 的 Review Agent。接入仓库负责维护实际 CI 脚本、工具依赖、审核规则和 GitHub Ruleset。

## 执行与幂等

仓库启用 `preflight` 只代表具备本地 CI 能力。编排器仅在匹配规则配置 `run_preflight: true`、仓库启用 CI 且变更请求仍处于打开状态时执行门禁。规则要求 CI 但仓库未启用或未配置时直接启动 Agent，不报错；PR 已关闭或合并时也跳过门禁。规则选择全部仓库时，不要求所有启用仓库都配置 CI。

同一批次中未要求 CI 的 Agent 会立即启动，不等待 Preflight。要求 CI 的规则共享以仓库 ID、PR 编号、Head SHA 和完整配置 revision 生成的幂等结果。`success`、`failure` 与 `timed_out` 是可复用的本地终态；Git、进程启动或清理故障产生的 `error` 只影响要求 CI 的事件路径，并按事件重试额度再次执行。基础设施异常耗尽自动重试后，扫描事件继续复用该终态以避免无限运行；管理员新建的手动事件会把旧运行保留为历史记录，并为同一幂等键原子换入一条全新运行和完整重试额度。新运行随后成为同一 Head 与配置的当前可复用结果。

执行器先更新 PR 引用并创建独立 detached worktree，校验实际 Head SHA、初始化 submodule；精确工作区准备成功后才写入 `pending` Commit Status，并按参数数组顺序执行步骤。命令不经过 shell；单步超时同时受总超时约束，首个非零退出码停止后续步骤。日志按 `max_output_bytes` 保留最新内容。Preflight worktree 与 Agent 运行 clone/worktree 相互独立，均不修改基础仓库。

`cache_enabled: true` 时，依赖下载缓存按仓库稳定身份隔离并跨分支共享，避免同一仓库的不同 PR 重复下载；临时 worktree、HOME 与安装目录不共享。系统会为常见 Python、Node.js、Rust、Go、Java、.NET、PHP 和浏览器工具注入其标准缓存变量。仓库详情还可以手动执行远端默认分支最新提交，用于提前填充缓存；手动运行不设 CI 步骤总限时，但可以取消，且不会触发 Agent 或发布 PR Commit Status。

所有新运行都会把 Git 准备阶段、当前步骤状态及合并后的 stdout/stderr 按游标写入实时日志。仓库手动执行入口、运行概览事件详情和“运行与日志”页复用同一详情视图；终态仍额外固化有界完整输出，旧记录没有实时日志时自动回退到终态输出。

本地命令终态和 Commit Status 发布标记分别持久化。终态已经生成但 GitHub 回写失败时，后续重试只补发状态，不重新执行仓库命令。

开启失败评论后，同一 PR 与 `status_context` 只保留一条活动 Preflight 评论。新的真实失败或超时会先删除旧评论，再在时间线底部创建本轮评论；相同 CI 结果被其他事件复用时不会反复刷新，只有本地映射缺失时才补建。当前最新源版本的真实或复用成功结果都会清理该槽位全部历史代次失败评论，历史旧代次成功不触碰新 Head 评论。该生命周期只适用于 Preflight，Agent 托管评论仍按源版本代次保留审核历史。

## 状态映射

- `success`：写入 GitHub `success`，继续启动要求 CI 的 Review Agent。
- `failure`：写入 GitHub `failure`，正常结束要求 CI 的规则路径；不影响同批次未要求 CI 的 Agent。
- `timed_out`：按代码失败处理并写入 GitHub `failure`，只阻断要求 CI 的规则。
- `error`：不写入 GitHub Commit Status，也不发布或改写失败评论；只让要求 CI 的事件路径失败并按 `event_retry_count` 重试。

启用门禁的仓库会自动产生首次 `change_request.discovered` 事件，不受全局 `scanner.emit_initial_events` 开关影响。部署方应在 GitHub Ruleset 中把 `status_context`（默认 `teamwork/local-ci`）设为 required status check。

## 凭据与安全边界

CI 子进程仅继承工具运行所需的 PATH、临时目录、语言、证书、代理和缓存变量，并注入 `CI=true`；`HOME` 指向一次性空目录。Provider Token、Codex/OpenAI 凭据及其他未列入白名单的环境变量不会进入被测代码。Provider Token 只由后台确定性接口用于扫描和 Commit Status 回写。

临时 worktree 与环境过滤只用于隔离工作目录和避免凭据意外继承，不是容器或操作系统级安全边界。当前威胁模型仅覆盖可信内部成员提交的 PR；检查 fork 或其他不可信代码前，应迁移到独立 UID、容器或虚拟机，并限制挂载、进程与网络。

当前只支持 GitHub 状态回写。每个步骤是一条不经过 shell 的参数数组命令；简单检查可以直接配置程序与参数，复杂流程可以调用目标仓库维护的 Bash、Python 或其他脚本。原生 Windows 可以直接执行 Python、Node.js、PowerShell 等程序；配置为 `bash ci/preflight.sh` 的步骤仍要求 Git Bash、WSL2 或其他可用的 Bash。仓库详情提供步骤名称、执行程序、逐项参数和超时配置，触发规则详情负责选择是否使用仓库 CI。
