# Agent 角色超时与远端 CI 等待设计

## 已确认行为

- 所有根运行（事件、手动、定时）不再使用 `Agent.timeout_seconds` 总时限；子运行仍使用该配置。角色由父运行 ID 判断，不按 Agent 名称特判。
- 根运行保留无进展超时。等待子任务由子任务自行限时；受控远端 CI 等待不计入普通 idle。
- 远端 CI 等待默认 1800 秒，可全局配置并由仓库覆盖。首次进入等待起计时，排队、执行均包含，轮询不刷新期限。
- 运行阶段超时不可自动从头重放；保留工作区、分支和 PR，标记超时待处理。准备阶段重试和模型内部 API 回退维持原行为。
- 本次不实现断点续跑，不恢复或合并历史 PR，不中断当前真实任务。

## 实现边界

新增只读 `wait_for_ci(number, expected_head_sha)` 工具，仅访问可信调用上下文所对应仓库，不能传入任意 URL、凭据或执行命令。使用仓库级优先的平台 Token。GitHub 查询当前 PR、Check Runs 和 Commit Status；GitLab 查询 MR 当前 pipeline。分页不能漏读；权限错误、缺失或未知状态不得当作通过。成功只表示可观察 CI 完成，不代表审批、分支保护及合并授权通过；最终合并仍需再次核验。

CI 等待状态持久化到 SQLite，按本次运行及 PR/head 标识固定截止时间。模型循环通过运行控制暂停 idle；完整 CLI 通过服务持久化的受控等待状态判断，不根据任意 shell 文本猜测等待。CI 等待到期持久化终止原因，阻止后续模型/工具再执行清理、推送或合并。传输时限保留额外收尾宽限，不能抢先于 CI 等待到期。取消与服务停止保持有效。

更新内置组合入口的等待规则，使用工具而非长期 shell 轮询；统一运行时说明同时覆盖其他 Agent，超时保留成果的约束优先于旧的失败清理段落。自定义 Prompt 不会自动改写，但收到相同运行时工具说明。平台 API 仅使用当前仓库的固定路径，依据 [GitHub Check Runs](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference)、[Commit Status](https://docs.github.com/en/rest/commits/statuses#get-the-combined-status-for-a-specific-reference) 与 [GitLab MR](https://docs.gitlab.com/api/merge_requests/#get-single-mr) 接口。

UI 明确区分子 Agent 总时限、Agent 无进展时限、远端 CI 等待与本地 Preflight 执行超时；运行详情展示等待状态和超时原因。

## 验证与风险

隔离测试覆盖根运行越过旧总时限、子运行总时限、等待结束后 idle 恢复、取消、嵌套等待、CI 轮询/分页/错误/空结果/源 SHA 漂移、固定截止时间、超时不继续模型调用或整任务重试。CLI 普通 MCP、托管 Broker 和 Windows 独立代理均保持工具一致。

不以 CI 工具的成功结果替代平台合并门禁。旧配置兼容；已有运行需要服务升级后新启动才生效。本次不重启真实服务，避免取消现有任务。
