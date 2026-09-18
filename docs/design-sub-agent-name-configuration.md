# 子 Agent 名称配置一致性修复设计

## 问题

组合更新入口已改用 `DEPENDENCY_REVIEWER_AGENT_NAME` 和 `INCREMENTAL_DOC_UPDATER_AGENT_NAME`，但配置示例、使用说明和部分测试仍使用旧名称，导致 PR #30 的两项测试失败。按旧示例配置时，子 Agent 名称会渲染为空，组合入口按既有规则停止。

## 方案与边界

- 保留入口 Prompt 的新变量名称，不回滚改名，不新增旧名称自动回退。
- 同步 `config_example.yaml`、README、架构说明和相关测试；默认值仍分别为 `dependency-reviewer`、`incremental-doc-updater`。
- 保持变量仅向 Prompt 暴露、不进入进程的配置，以及现有子 Agent 白名单不变。
- 通过真实示例配置加载、运行环境解析、入口 Prompt 渲染的联动测试，验证两个名称均非空、来自配置并且属于允许调用的 Agent。
- 增加自定义名称与缺失变量测试，避免通过硬编码默认名称或自动读取旧名称掩盖配置缺失。
- README 说明旧部署须在配置该变量的全局、Agent 或仓库环境中改名；保留原值和暴露设置。不自动修改真实 `config.yaml`，不重启服务，不触发 Agent 或修改平台 PR。
- 历史实施记录保留原始叙述；被忽略的本地独立入口不纳入本次发布。

## 验收

原两项失败测试通过；示例配置可渲染有效子 Agent 名称；相关回归与全量 Python 测试通过。检查新增文件和完整差异后提交并推送当前分支。
