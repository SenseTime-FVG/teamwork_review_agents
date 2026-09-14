# 后台启动确认实施方案

1. 统一进程管理默认启动预算为 30 秒，校验显式值。
2. 为 `start` / `restart` 增加启动确认参数并传递给后台启动器。
3. 健康请求绕过环境代理，返回明确的连接、协议或 PID 诊断；启动循环记录最后等待条件。
4. 添加慢启动、CLI 参数、直连检查、失败原因和子进程清理测试，运行进程管理与 CLI 回归。
5. 更新 README 与运维说明，记录默认时间、可用参数及排障方法；检查差异后提交推送。

## 验收记录

已运行 `python -m pytest -o addopts='' tests/test_process_manager.py tests/test_config_and_runner.py -q`，92 项测试通过；CLI 帮助、Python 编译检查及 `git diff --check` 通过。验证包含本机真实启停与模拟慢启动；本次未在原生 Windows 或 WSL 环境实测。
