# 贡献指南

## 环境

- Linux 或 macOS；
- Python 3.11+；
- Node.js 与 npm；
- Git。

安装后端和测试依赖：

```bash
python -m pip install -e '.[dev]'
```

安装前端依赖：

```bash
cd ui
npm install
```

## 本地开发

准备配置并启动后端：

```bash
cp config_example.yaml config.yaml
teamwork-review-agents validate
teamwork-review-agents run
```

前端联调：

```bash
cd ui
npm run dev
```

Vite 会把 `/api` 转发到 `127.0.0.1:8080`，构建结果写入 `src/teamwork_review_agents/web/dist`。

## 验证

后端测试：

```bash
pytest
```

前端类型检查与构建：

```bash
cd ui
npm run build
```

只改文档时，至少检查 Markdown 链接和 `git diff --check`。

## 目录

| 路径 | 内容 |
| --- | --- |
| `src/teamwork_review_agents/` | 后端、扫描、事件、执行器和管理 API |
| `src/teamwork_review_agents/providers/` | GitHub / GitLab 适配器 |
| `ui/src/` | React 管理界面 |
| `prompts/` | 内置 Agent Prompt |
| `tests/` | 后端测试 |
| `docs/` | 架构、设计、运维和实施记录 |
| `deploy/` | systemd / launchd 模板 |

## 修改原则

1. 先阅读 [`docs/design.md`](docs/design.md) 和相关测试。
2. 保持 GitHub / GitLab 统一模型和事件幂等。
3. 权限、凭据、锁、超时和清理逻辑必须有测试。
4. 不把 Token、账号信息、本地配置或运行数据库提交到仓库。
5. 用户可见行为、命令或配置变化时，同步更新 README、配置示例和相关文档。
6. 前端变化提交前运行 `npm run build`，后端变化提交前运行相关 `pytest`。

## 文档

- [README](README.md)：首次使用；
- [运维与排障](docs/operations.md)：部署、权限和故障处理；
- [架构](docs/architecture.md)：系统边界与 Agent 流程；
- [系统设计](docs/design.md)：精确实现语义；
- [实施方案](docs/implementation-plan.md)：历史阶段与验收记录。
