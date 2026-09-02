# 运维与排障

本文面向部署和维护 Teamwork Review Agents 的人员。完整字段见 [`config_example.yaml`](../config_example.yaml)，内部实现见 [`design.md`](design.md)。

## 1. 运行前提

- Linux、macOS、原生 Windows 或 WSL2；
- Python 3.11+、Git、Codex CLI；
- 目标仓库的 SSH Key 或 HTTPS 凭据；
- GitHub / GitLab Provider Token；
- Agent 需要平台写操作时，同一系统用户需完成 `gh` / `glab` 登录。

运行端、Git 凭据、Codex 登录和 CLI 登录都应属于同一系统用户。

## 2. 凭据边界

| 凭据 | 用途 | 是否传给 Codex |
| --- | --- | --- |
| Provider Token | 扫描 GitHub / GitLab API | 否 |
| Codex 登录 | 运行 `codex exec` | 由 Codex Home 管理 |
| `gh` / `glab` 登录 | 评论、推送、创建或合并 PR / MR | CLI 自行读取 |
| 管理员 Token | 保护管理 API | 否 |

Provider Token 按仓库环境、全局环境、服务进程宿主机环境的顺序读取同名变量。仓库层和全局层推荐使用 `from_system`，避免把真实值写入 `config.yaml`；某层已经配置但解析为空时不会向更宽权限层静默降级。

写平台操作使用单独的最小权限身份，不要把 Provider Token 重新注入 Agent 环境。

## 3. Codex 运行时

`runtime.codex_home` 留空时，服务使用当前 `CODEX_HOME` 或 `~/.codex`。配置独立目录后，可在管理界面完成登录，并与桌面或终端 Codex 隔离。

常用检查：

- `runtime.codex_binary` 指向预期 CLI；
- `runtime.expected_codex_version` 与实际版本一致；
- 独立 Codex Home 已登录；
- 模型和推理参数已保存，而不是只停留在编辑状态。

## 4. 启停方式

| 命令 | 行为 |
| --- | --- |
| `teamwork-review-agents start` | 后台启动；健康接口确认新 PID 后才返回成功 |
| `teamwork-review-agents run` | 前台运行；适合调试和进程管理器 |
| `teamwork-review-agents stop` | POSIX 先请求优雅停止，Windows 终止受管进程树；等待 30 秒后仍未退出则强制结束 |
| `teamwork-review-agents restart` | 停止旧实例，确认退出后启动新实例 |
| `teamwork-review-agents scan-once` | 扫描、生成事件并等待 Agent 执行完成 |
| `teamwork-review-agents scan-once --dry-run` | 保存快照和事件，但不启动 Agent |
| `teamwork-review-agents runs --limit 20` | 查看最近运行 |

所有命令默认读取当前目录的 `config.yaml`。自定义配置必须始终传入同一路径：

```bash
teamwork-review-agents start -c /path/to/config.yaml
teamwork-review-agents restart -c /path/to/config.yaml
teamwork-review-agents stop -c /path/to/config.yaml
```

Windows PowerShell 使用 Windows 路径：

```powershell
teamwork-review-agents start -c C:\path\to\config.yaml
teamwork-review-agents restart -c C:\path\to\config.yaml
teamwork-review-agents stop -c C:\path\to\config.yaml
```

## 5. 运行文件

默认配置的运行文件位于 `config.yaml` 同目录的 `data/`：

- `teamwork-review-agents.pid`：进程身份；
- `teamwork-review-agents.lock`：单实例锁；
- `teamwork-review-agents.log`：后台标准输出和错误；
- SQLite 数据库：快照、事件、运行和 Agent 日志；
- `worktrees/`：每次 Agent 的临时工作区。

自定义配置文件会使用带配置名和短哈希的文件名前缀，避免相互覆盖。

服务运行时不要移动或删除 SQLite 数据库及其 `-wal`、`-shm` 文件。

## 6. 生产部署

生产环境建议让进程管理器执行前台命令：

```bash
teamwork-review-agents run -c /path/to/config.yaml
```

模板：

- [systemd](../deploy/teamwork-review-agents.service)
- [launchd](../deploy/com.teamwork.review-agents.plist)

systemd 与 launchd 模板不适用于 Windows。Windows 可让服务管理器或任务计划程序执行：

```powershell
teamwork-review-agents run -c C:\path\to\config.yaml
```

确保服务用户拥有配置、数据库、基础仓库和临时 Git 工作区目录权限，并能读取所需的 Codex、Git、SSH、`gh` 或 `glab` 凭据。

## 7. 扫描语义

- 服务启动后立即扫描，之后按 `scanner.interval_seconds` 运行。
- 首次扫描只回看一个扫描周期；更早的 PR / MR 只建立快照。
- `scanner.emit_initial_events` 只控制是否额外产生 `change_request.discovered`。
- dry-run 生成的待处理事件会在后续普通运行中继续处理。
- GitHub Timeline 可恢复部分扫描间隔内的离散动作；GitLab 主要依赖快照差异。
- `deduplicate_per_scan: true` 只合并同轮 Agent 调度，不删除原始事件。

管理界面中的“已扫描 PR / MR”来自快照，“变化事件”来自状态变化，两者不是同一个计数。

## 8. Agent 隔离与清理

每次根 Agent 默认使用独立 Git 工作区：声明 `workspace` 写操作的 Agent 使用本地 clone，只读 Agent 使用 detached worktree。声明 `workspace` 写操作后，同一源分支串行；声明 `change_request` 写操作后，同一 PR / MR 串行。

成功且干净的 clone/worktree 会立即删除。以下情况会保留，便于排查或恢复：

- Agent 失败或超时；
- 存在未提交修改；
- 新提交尚未出现在远端。

保留期限由 `runtime.worktree_retention_days` 控制，默认 7 天。到期后会在下次准备同一仓库时清理。

`timeout_seconds` 控制总时长，`agent_idle_timeout_seconds` 控制连续无 JSONL 进展时长。管理界面可以取消排队中或执行中的任务。

## 9. 文件权限与联网

`sandbox` 控制本地文件权限，`network_access` 控制 `workspace-write` 命令联网，二者独立。

```yaml
agents:
  reviewer:
    sandbox: workspace-write
    network_access: true
    network_domains:
      - api.github.com
      - "*.github.com"
```

- `network_domains` 为空：允许访问公开网络；
- 非空：只允许精确域名或受支持的通配域名；
- `read-only` 不能通过该开关开放命令联网；
- `danger-full-access` 不受域名白名单可靠隔离。

Teamwork 默认关闭用户 Codex 配置中的 MCP Server，只保留应用托管的 `invoke_agent` 网关。确有需要时使用 `runtime.allowed_user_mcp_servers` 显式放行。

## 10. 管理界面认证

监听 `127.0.0.1` 时可不配置 Token。对外监听必须设置：

```yaml
web:
  host: 0.0.0.0
  port: 8080
  admin_token_env: TEAMWORK_ADMIN_TOKEN
```

```bash
export TEAMWORK_ADMIN_TOKEN='高强度随机值'
teamwork-review-agents run
```

在管理界面输入同一 Token。除 `/api/health` 外，管理 API 都会校验它。

## 11. 排障

| 现象 | 检查 |
| --- | --- |
| `start` 失败 | 查看 `data/teamwork-review-agents.log`；检查端口和配置校验输出 |
| 健康接口不是当前 PID | 执行 `stop` 或 `restart`；不要按端口直接杀进程 |
| PID 或锁文件丢失 | `stop` / `restart` 会按配置路径发现托管进程 |
| 仓库没有被扫描 | 检查 Provider Token、仓库 `enabled`、远端项目和 API 地址 |
| 有事件但没有 Agent | 检查规则 `enabled`、事件名、仓库和条件 |
| Agent 无法访问平台 | 用服务用户执行 `gh auth status` / `glab auth status`，再检查 `network_access` |
| Codex 立即失败 | 检查 CLI 路径、Codex Home 登录和期望版本 |
| Agent 超时 | 查看运行消息，区分总超时和无进展超时 |
| 临时 Git 工作区未删除 | 查看工作区状态和保留原因，不要直接删除仍在使用的目录 |

无法判断时，先保留数据库和临时 Git 工作区，再从管理界面的运行详情、后台日志和健康接口定位问题。
