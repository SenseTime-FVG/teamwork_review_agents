# Windows 后台子进程隐藏窗口

## 问题与范围

服务本身通过 `DETACHED_PROCESS` 启动，但 Git、工具、Codex CLI 和 MCP 等普通子进程只有 `CREATE_NEW_PROCESS_GROUP`。后台服务没有控制台时，启动控制台程序可能出现窗口闪现。另外，快照、模型目录、版本和沙盒能力检查直接调用 `subprocess.run()`，未经过公共进程组参数。

## 设计

- 新增仅负责窗口行为的 `hidden_process_options()`：Windows 返回 `CREATE_NO_WINDOW`，POSIX 返回空字典，避免同步诊断命令被额外创建会话。
- `process_group_options()` 保留独立进程组；Windows 普通子进程叠加隐藏窗口参数，后台服务仍使用 `DETACHED_PROCESS`，不与 `CREATE_NO_WINDOW` 混用。POSIX 继续返回 `start_new_session=True`。
- 优先读取 `subprocess.CREATE_NO_WINDOW`，非 Windows 导入与模拟测试所用兼容值为官方常量 `0x08000000`。
- 为所有直接启动的同步诊断和 Git 快照命令补上隐藏窗口参数。现有输出管道、退出码、超时、终止进程树和重试策略不变。
- 不增加 UI 开关，不修改凭据或排查范围外的 `WinError 5`。

## 边界

只控制 Teamwork 创建的子进程；外部程序主动打开 GUI、创建独立控制台或另行启动后代进程不受此标志统一约束。Windows 桌面的实际闪窗现象仍需部署端验收。

## 验证

使用平台模拟验证精确标志组合和同步调用覆盖；使用原生 Windows 测试检查无控制台后台父进程启动的同步、异步子进程没有控制台窗口且管道输入输出、退出码正常。保留并运行进程树取消、超时和服务生命周期回归测试。
