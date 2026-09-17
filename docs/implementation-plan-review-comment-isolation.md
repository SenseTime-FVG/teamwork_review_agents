# 通用审核的评论读取隔离实施方案

## 实施步骤

1. 核对通用审核 Agent 的 Prompt 文件引用与运行器加载方式，不修改真实配置。
2. 删除审核上下文中“已有讨论”的读取要求，明确保留 GitHub `body` 与 GitLab `description`。
3. 增加禁止读取各类评论、评审正文和讨论的规则，覆盖 CLI、API、连接器和网页入口；提供不包含评论字段的 GitHub 查询示例。
4. 保留审批、讨论解决状态、CI 和其他平台门禁检查，以及本轮审核评论发布规则。
5. 补充模板渲染和运行器输入契约测试，记录测试结果并更新文档索引。

## 验收

- 自动合并、仅审核和未配置开关时均保留相同的评论读取边界。
- 标题和描述继续读取，空描述不得回退到第一条评论。
- 不再要求读取已有讨论，也不通过历史审核评论决定本轮完成状态。
- 门禁元数据与评论正文明确区分，不能默认跳过未确认的门禁。
- 托管评论发布规则不变，压缩实现和自定义 Prompt 不变。
- 相关测试和 `git diff --check` 通过后，仅提交本次文件并推送当前分支。

## 验证记录

- 已确认本地 `general-reviewer` 使用 `./prompts/general-review.md`，运行器在构造输入时读取文件；未修改配置。
- 新增 5 项参数化用例全部通过：3 种自动合并配置的模板边界，以及 2 种模式下的运行器文件加载。
- 回归范围：`test_prompt_rendering.py`、`test_prompt_language.py`、`test_config_and_runner.py`、`test_managed_comments.py`、`test_context_compaction.py`。
- 上述范围发现 2 项既有失败：`test_builtin_prompts_render_configured_environment_values_directly` 和 `test_combined_update_prompts_support_github_and_gitlab`。失败都发生在组合更新入口的 Agent 名称变量断言，使用修改前 HEAD 的测试代码也能复现；未改动该入口模板或这些断言。
- 显式排除上述 2 项既有失败后，实际结果为 `268 passed, 2 deselected`；新增用例包含在通过项中。不能将此结果描述成完整回归全绿。
- `git diff --check` 通过。未请求真实模型或触发真实审核，测试只验证 Prompt 与运行器输入契约。
