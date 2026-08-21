# 概览与运行记录多状态筛选设计

## 背景

运行概览中的“已扫描 MR / PR”已经支持按多个状态筛选，但“最近变化事件”和“运行与日志”仍然只能选择一个状态。排查事件处理和执行结果时，用户经常需要同时查看多个状态，单选会增加重复切换成本，也不利于组合查看成功、失败或进行中的记录。

## 目标

- “已扫描 MR / PR”“最近变化事件”和“运行与日志”的状态筛选均支持多选。
- “全部状态”与具体状态互斥。
- 多个具体状态之间按 OR 语义查询。
- 保持既有单个 `status` 或 `status_group` 查询参数兼容。

## 交互规则

1. 未选择具体状态时等同于“全部状态”。
2. 点击“全部状态”会清空所有具体状态。
3. 从“全部状态”切换到具体状态时，只选中被点击的状态。
4. 具体状态之间可以任意组合。
5. 取消最后一个具体状态后自动恢复为“全部状态”。
6. 下拉触发器在单选时显示状态名称，多选时使用顿号连接所选状态。

## 接口约定

概览列表通过重复 `status` 查询参数表达多选：

```text
GET /api/change-requests?status=opened&status=merged
GET /api/events?status=processing&status=failed
```

运行记录通过重复 `status_group` 查询参数表达语义状态组多选：

```text
GET /api/runs?status_group=running&status_group=failure
GET /api/preflight-runs?status_group=running&status_group=failure
```

未提供状态参数表示全部状态。原有单个参数仍然有效。

“运行与日志”同时展示 Agent 与本地 Preflight / CI。前端选择的是统一语义状态组，后端分别将其映射为两类记录的实际状态，再使用集合查询：

- Agent 的“等待中”映射为 `queued` 和 `preparing`。
- Preflight / CI 的“失败”映射为 `failure` 和 `error`。
- Preflight / CI 没有“等待中”记录，因此只选择“等待中”时不会返回 CI 记录。

## 数据查询

状态集合非空时，快照、事件与运行记录使用参数化 `IN` 条件。事件列表与事件总数必须使用同一组条件，保证分页数据与总数一致。状态集合为空时不添加状态条件。

## 非目标

- 不改变 MR / PR、事件或运行记录的状态枚举。
- 不改变扫描器保存快照的逻辑。
- 不改变 Agent 与 Preflight / CI 的状态生命周期。
