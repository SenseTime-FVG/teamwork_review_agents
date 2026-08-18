# GitHub Preflight CI 门禁

## 职责边界

通用引擎负责准确检出 PR Head、隔离执行、超时与日志控制、结果幂等、GitHub Commit Status 回写，以及成功后启动 Review Agent。接入仓库负责维护实际 CI 脚本、工具依赖、审核规则和 GitHub Ruleset。

## 执行与幂等

仓库启用 `preflight` 后，编排器只在存在匹配规则且变更请求仍处于打开状态时执行门禁。执行器以仓库 ID、PR 编号、Head SHA 和完整配置 revision 生成幂等键。`success`、`failure` 与 `timed_out` 是可复用的本地终态；Git、进程启动或清理故障产生的 `error` 按事件重试额度再次执行。

执行器先写入 `pending` Commit Status，再更新 PR 引用并创建独立 detached worktree，校验实际 Head SHA、初始化 submodule，然后按参数数组顺序执行步骤。命令不经过 shell；单步超时同时受总超时约束，首个非零退出码停止后续步骤。日志按 `max_output_bytes` 保留最新内容。Preflight worktree 与 Agent worktree 相互独立，均不修改基础仓库。

本地命令终态和 Commit Status 发布标记分别持久化。终态已经生成但 GitHub 回写失败时，后续重试只补发状态，不重新执行仓库命令。

## 状态映射

- `success`：写入 GitHub `success`，继续启动匹配的 Review Agent。
- `failure`：写入 GitHub `failure`，正常结束事件且不启动 Agent。
- `timed_out`：按代码失败处理并写入 GitHub `failure`。
- `error`：写入 GitHub `error`，事件失败并按 `event_retry_count` 重试。

启用门禁的仓库会自动产生首次 `change_request.discovered` 事件，不受全局 `scanner.emit_initial_events` 开关影响。部署方应在 GitHub Ruleset 中把 `status_context`（默认 `teamwork/local-ci`）设为 required status check。

## 凭据与安全边界

CI 子进程仅继承工具运行所需的 PATH、临时目录、语言、证书、代理和缓存变量，并注入 `CI=true`；`HOME` 指向一次性空目录。Provider Token、Codex/OpenAI 凭据及其他未列入白名单的环境变量不会进入被测代码。Provider Token 只由后台确定性接口用于扫描和 Commit Status 回写。

临时 worktree 与环境过滤只用于隔离工作目录和避免凭据意外继承，不是容器或操作系统级安全边界。当前威胁模型仅覆盖可信内部成员提交的 PR；检查 fork 或其他不可信代码前，应迁移到独立 UID、容器或虚拟机，并限制挂载、进程与网络。

当前只支持 GitHub 状态回写和 YAML 配置；具体 CI 命令、工具安装与审核规则属于目标仓库。
