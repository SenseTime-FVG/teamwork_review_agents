# Provider Token 默认进入运行进程实施方案

## 配置层

- 调整 Provider 凭据保护逻辑：未声明的 `expose_to_process` 默认补为 `true`，明确的 `false` 保持不变。
- 调整新增平台 Provider 时的同名变量初始化：强制 Secret、关闭 Prompt、开启进程。
- 保持运行时环境解析顺序与脱敏逻辑不变。

## 管理页面

- Provider Token 新成为受保护变量时默认选中“进程”。
- 保持“Prompt”默认关闭和“Secret”锁定开启。
- 更新全局、仓库和 Agent 页面说明，明确仓库 Token 默认供 Agent、工具命令和 CI 使用。

## 文档与示例

- 更新 README、首次使用、运维、CLI 认证和本地 CI 文档。
- 更新示例配置，使 Provider Token 示例与新默认一致。
- 明确 `gh` / `glab` 的程序仍来自本机，认证优先使用进程环境中的仓库 Token。

## 验证

- 单元测试覆盖缺省 Provider Token 自动进入进程但不进入 Prompt。
- 单元测试覆盖普通变量新成为 Provider Token 时采用新默认。
- 保留显式关闭进程暴露的回归测试。
- 构建前端静态资源并运行相关 Python 测试与完整测试集。
