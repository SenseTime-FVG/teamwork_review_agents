# Prompt 输出语言配置设计

## 目标与最终约定

所有现有 Markdown Prompt 都增加统一的「使用语言」章节。章节中的语言优先读取当前 Agent 专用变量，其次读取 `LANGUAGE`；未配置、空字符串或仅含空白时继续回退，最终默认中文。

此约定取代最初讨论的「没有配置就不添加语言章节」。章节始终存在，内容如下：

```markdown
# 使用语言

英文

- 始终使用该语言进行回复、撰写报告或发表评论。
```

原有写死中文的输出要求改为引用「使用语言」，其他任务规则、固定状态值和已有文档语言约束不变。

## 变量映射

| Prompt | Agent 专用变量 |
| --- | --- |
| `general-review.md` | `GENERAL_REVIEWER_LANGUAGE` |
| `依赖review.md` | `DEPENDENCY_REVIEWER_LANGUAGE` |
| `依赖review 入口.md` | `DEPENDENCY_REVIEW_RUNNER_LANGUAGE` |
| `增量文档更新.md` | `INCREMENTAL_DOC_UPDATER_LANGUAGE` |
| `增量文档更新入口.md` | `INCREMENTAL_DOC_UPDATE_RUNNER_LANGUAGE` |
| `依赖review&增量文档更新 入口.md` | `DEPENDENCY_AND_INCREMENTAL_DOC_UPDATE_RUNNER_LANGUAGE` |

两个独立入口是当前工作区中已被 Git 忽略的本地 Prompt；此次同步修改其模板，但不将其强制加入版本控制，也不增加内置 Agent 或规则。

## 实现边界

- 在现有沙盒 Jinja 环境中新增 `prompt_language` 过滤器。每个模板显式传入自己的专用变量及 `LANGUAGE`，不依赖文件名或 Agent 名称推断。
- 支持 `中文`、`英文`、`zh`、`en`、`zh-CN`、`en-US`、`Chinese`、`English`，忽略首尾空白和英文大小写，统一渲染成「中文」或「英文」。
- 优先级最高的非空值若非法，抛出明确的 Prompt 配置错误，不悄悄改用其他语言，也不回显原始值。
- 仅使用现有「传给 Prompt」的变量上下文，不读取宿主机隐式 `LANGUAGE`，不要求模型执行命令读取环境，不改动「传给进程」设置。
- 不改变同名环境变量原有的全局、Agent、仓库覆盖顺序；此处优先级是专用变量与 `LANGUAGE` 这两个不同变量之间的选择。
- 主 Agent 与子 Agent 分别渲染自己的 Prompt；主 Agent 的专用语言变量不作为子 Agent 的语言默认值。
- 保留组合入口中原有未提交的子 Agent 名称变量改动，本次只提交语言相关差异。

## 验收

验证全部现有模板的默认中文、专用变量优先、空值逐级回退、别名规范化、非法配置报错、预览与执行一致，以及不暴露给 Prompt 的变量不参与语言选择。确保协议状态值和文档原语言约束未被改写。
