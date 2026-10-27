"""CI 失败诊断的配置、去重、失败隔离与评论生命周期回归。"""

from __future__ import annotations

import asyncio
import html
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from teamwork_review_agents.builtin_agents import CI_DIAGNOSIS_AGENT
from teamwork_review_agents.ci_diagnosis import diagnose_ci_failure
from teamwork_review_agents.ci_diagnosis_tools import execute_ci_diagnosis_tool
from teamwork_review_agents.config_manager import ConfigManager
from teamwork_review_agents.config import RuleConfig
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.executor import AgentExecutor
from teamwork_review_agents.managed_comments import ManagedCommentService
from teamwork_review_agents.models import AgentResult, InvocationContext, PreflightResult
from teamwork_review_agents.orchestrator import CycleSummary, Orchestrator
from teamwork_review_agents.preflight import PreflightExecutor, build_failure_comment_body
from teamwork_review_agents.state import StateStore


@pytest.fixture
def diagnosis_case(configured_app_factory, snapshot_factory, monkeypatch):
    """创建仅使用临时数据库和虚构凭据的真实失败 CI。"""

    config = configured_app_factory()
    config.runtime.codex.model = "gpt-5.6-terra"
    settings = config.repositories[0].preflight
    # 不显式开启诊断，覆盖配置缺省时的实际运行行为。
    settings.publish_failure_comment = False
    monkeypatch.setenv("GITHUB_TOKEN", "diagnosis-test-secret")
    snapshot = snapshot_factory(provider="github-main")
    event = detect_events(None, snapshot, emit_initial=True)[0].model_copy(update={"source_generation": 1})
    store = StateStore(config.database.path)
    store.initialize()
    store.save_snapshot_and_events(snapshot, [event])

    def create_run(run_id="ci-one", status="failure"):
        """创建同一提交的新检查记录，支持验证迟到报告。"""

        store.begin_preflight_run(
            proposed_run_id=run_id, idempotency_key=run_id, event_id=event.id,
            repository_id="demo", number=7, head_sha=snapshot.head_sha,
            config_revision=config.revision, max_attempts=3,
        )
        result = PreflightResult(
            run_id=run_id, repository_id="demo", number=7, head_sha=snapshot.head_sha,
            status=status, failed_step="test-all", exit_code=1, output="AssertionError: 预期拒绝，实际成功\n",
        )
        store.finish_preflight_run(result)
        return result

    result = create_run()
    execute = AsyncMock(return_value=AgentResult(
        run_id="diagnostic-agent-run", root_run_id="diagnostic-agent-run",
        agent_name=CI_DIAGNOSIS_AGENT, status="completed", final_message="## 错误报告\n测试断言失败。",
    ))
    return SimpleNamespace(config=config, store=store, event=event, result=result,
                           executor=SimpleNamespace(execute=execute), create_run=create_run)


async def diagnose(case):
    """通过生产调度入口执行诊断。"""

    return await diagnose_ci_failure(case.config, case.store, case.executor, case.event, case.result)


@pytest.mark.parametrize("configured", [None, False, True])
def test_ci_diagnosis_default_and_explicit_choice_survive_reload(configured_app_factory, configured):
    """旧仓库缺省诊断开启，明确关闭可跨 CI 开关切换、保存和重启保留。"""

    config = configured_app_factory()
    manager = ConfigManager(config.config_path)
    document = manager.document()
    preflight = {"enabled": True, "steps": [{"name": "test", "command": ["pytest"]}]}
    if configured is not None:
        preflight["failure_diagnosis_enabled"] = configured
    document["repositories"][0]["preflight"] = preflight
    expected = configured is not False
    for enabled in [True, False, True]:
        preflight["enabled"] = enabled
        manager.save(document)
        manager = ConfigManager(config.config_path)
        settings = manager.config.repositories[0].preflight
        assert settings.enabled is enabled
        assert settings.failure_diagnosis_enabled is expected
        assert settings.failure_diagnosis_agent == CI_DIAGNOSIS_AGENT


@pytest.mark.parametrize("explicit_reference", [False, True])
def test_builtin_can_rename_delete_and_remain_deleted(configured_app_factory, explicit_reference):
    """内置 Agent 可维护，删除后保留悬空引用，旧客户端保存与重启均不复活。"""

    config = configured_app_factory()
    manager = ConfigManager(config.config_path)
    document = manager.document()
    document["repositories"][0]["preflight"] = {"failure_diagnosis_enabled": True}
    if explicit_reference:
        document["repositories"][0]["preflight"]["failure_diagnosis_agent"] = CI_DIAGNOSIS_AGENT
    manager.save(document)
    agent = document["agents"][CI_DIAGNOSIS_AGENT]
    agent["prompt"] = "自定义 CI 分析提示词"
    manager.save_agent(expected_revision=manager.config.revision, name="ci-analysis",
                       original_name=CI_DIAGNOSIS_AGENT, agent=agent)
    assert manager.config.repositories[0].preflight.failure_diagnosis_agent == "ci-analysis"
    assert manager.config.agents["ci-analysis"].prompt == "自定义 CI 分析提示词"
    manager.delete_agent(expected_revision=manager.config.revision, name="ci-analysis")
    old_client = manager.document()
    old_client.pop("builtin_agents_version")
    assert CI_DIAGNOSIS_AGENT not in manager.validate(old_client).agents
    manager.save(old_client)
    restarted = ConfigManager(config.config_path)
    assert CI_DIAGNOSIS_AGENT not in restarted.config.agents
    assert "ci-analysis" not in restarted.config.agents
    assert restarted.config.repositories[0].preflight.failure_diagnosis_agent == "ci-analysis"
    assert restarted.document()["builtin_agents_version"] == 1


@pytest.mark.parametrize("status", ["success", "cancelled", "superseded", "running"])
async def test_non_failure_does_not_diagnose(diagnosis_case, status):
    """成功、取消、过期及执行中状态均不触发失败诊断。"""

    case = diagnosis_case
    case.store.finish_preflight_run(case.result.model_copy(update={"status": status}))
    assert not await diagnose(case)
    case.executor.execute.assert_not_called()
    assert case.store.get_preflight_diagnosis(case.result.run_id) is None


@pytest.mark.parametrize("disabled", ["setting", "manual"])
async def test_disabled_or_manual_ci_does_not_diagnose(diagnosis_case, disabled):
    """关掉诊断或默认分支手动 CI 不触发模型。"""

    case = diagnosis_case
    if disabled == "setting":
        case.config.repositories[0].preflight.failure_diagnosis_enabled = False
    else:
        with case.store.connect() as connection:
            connection.execute("UPDATE preflight_runs SET trigger_source='manual'")
    assert not await diagnose(case)
    case.executor.execute.assert_not_called()


async def test_deleted_agent_skips_without_fallback(diagnosis_case):
    """指定 Agent 已删除时只记录跳过，不能回退到其他审核 Agent。"""

    case = diagnosis_case
    del case.config.agents[CI_DIAGNOSIS_AGENT]
    assert not await diagnose(case)
    record = case.store.get_preflight_diagnosis(case.result.run_id)
    assert record["status"] == "skipped"
    assert "不存在或已删除" in record["error"]
    case.executor.execute.assert_not_called()


@pytest.mark.parametrize("status", ["failure", "timed_out"])
async def test_failure_diagnoses_once_and_preserves_ci(diagnosis_case, status):
    """真实失败和超时独立产生报告，并发事件和缓存复用均不能重复调用。"""

    case = diagnosis_case
    case.result = case.result.model_copy(update={"status": status})
    case.store.finish_preflight_run(case.result)
    results = await asyncio.gather(diagnose(case), diagnose(case))
    assert sorted(results) == [False, True]
    assert not await diagnose(case)
    case.executor.execute.assert_awaited_once()
    record = case.store.get_preflight_run(case.result.run_id)
    assert record["status"] == status
    assert record["diagnosis"]["status"] == "completed"
    assert "错误报告" in record["diagnosis"]["report"]
    assert case.executor.execute.call_args.kwargs["preflight_run_id"] == case.result.run_id
    assert case.executor.execute.call_args.kwargs["event"].current_snapshot.head_sha == case.result.head_sha


async def test_orchestrator_generates_report_without_starting_blocked_review(diagnosis_case):
    """完整编排保持失败门禁，只调用诊断角色并完成事件，不重跑 CI。"""

    case = diagnosis_case
    case.config.repositories[0].preflight.enabled = True
    case.config.rules = [RuleConfig(name="review", events=[case.event.type], agents=["code-reviewer"], run_preflight=True)]
    orchestrator = Orchestrator(case.config, recover_interrupted=False)
    orchestrator.executor = case.executor
    orchestrator.preflight = SimpleNamespace(ensure_passed=AsyncMock(return_value=case.result))
    summary = CycleSummary()
    await orchestrator.process_events(summary)
    case.executor.execute.assert_awaited_once()
    assert case.executor.execute.call_args.kwargs["agent_name"] == CI_DIAGNOSIS_AGENT
    assert summary.preflight_failures == 1
    assert orchestrator.store.pending_events() == []
    assert orchestrator.store.get_preflight_diagnosis(case.result.run_id)["status"] == "completed"


async def test_diagnosis_binds_stored_sha_even_when_event_has_new_current_snapshot(diagnosis_case):
    """旧事件的当前快照可被更新，报告必须仍读取失败 CI 的 SHA。"""

    case = diagnosis_case
    changed = case.event.model_copy(update={"current": case.event.current_snapshot.model_copy(update={"head_sha": "b" * 40})})
    with case.store.connect() as connection:
        connection.execute("UPDATE event_inbox SET payload=? WHERE event_id=?", (changed.model_dump_json(), case.event.id))
    assert await diagnose(case)
    assert case.executor.execute.call_args.kwargs["event"].current_snapshot.head_sha == case.result.head_sha


async def test_infrastructure_error_waits_for_last_retry(diagnosis_case):
    """Git 或进程故障只有在重试耗尽后才生成报告。"""

    case = diagnosis_case
    case.store.finish_preflight_run(case.result.model_copy(update={"status": "error"}))
    assert not await diagnose(case)
    with case.store.connect() as connection:
        connection.execute("UPDATE preflight_runs SET attempts=?", (case.config.runtime.event_retry_count + 1,))
    assert await diagnose(case)
    assert case.store.get_preflight_run(case.result.run_id)["status"] == "error"


async def test_model_error_is_redacted_and_does_not_change_ci(diagnosis_case):
    """诊断失败不覆盖 CI 失败，错误和报告中的凭据均脱敏。"""

    case = diagnosis_case
    case.executor.execute.side_effect = RuntimeError("模型错误 diagnosis-test-secret")
    assert await diagnose(case)
    record = case.store.get_preflight_run(case.result.run_id)
    assert record["status"] == "failure"
    assert record["diagnosis"]["status"] == "failed"
    assert "diagnosis-test-secret" not in record["diagnosis"]["error"]
    assert not await diagnose(case)


async def test_comment_failure_keeps_complete_redacted_report(diagnosis_case, monkeypatch):
    """远端评论失败只记录同步错误，完整报告仍可在 UI 复制。"""

    case = diagnosis_case
    case.config.repositories[0].preflight.publish_failure_comment = True
    case.executor.execute.return_value.final_message = "日志中的凭据 diagnosis-test-secret\n" + "报告" * 30000
    publish = AsyncMock(side_effect=RuntimeError("远端无权限 diagnosis-test-secret"))
    monkeypatch.setattr(ManagedCommentService, "publish_latest", publish)
    assert await diagnose(case)
    record = case.store.get_preflight_diagnosis(case.result.run_id)
    assert record["status"] == "completed"
    assert len(record["report"]) > 60000
    assert "diagnosis-test-secret" not in str(record)
    assert "远端无权限" in record["comment_error"]
    assert len(publish.call_args.kwargs["body"].encode()) < 60000


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_long_report_preserves_original_output_and_fold_structure(diagnosis_case, fence):
    """中文、HTML 转义和超长代码摘录共存时保留原日志、完整标签及截断提示。"""

    case = diagnosis_case
    result = case.result.model_copy(update={
        "output": "旧日志" * 10000 + "<&中文😀>" * 2000 + "最后一条失败信息 diagnosis-test-secret",
        "error": '错误 <&"' * 2000,
    })
    redactor = SecretRedactor(("diagnosis-test-secret",))
    original = build_failure_comment_body(case.config.repositories[0], result, redactor=redactor)
    report = f"## 失败原因\n\n{fence}text\n" + "堆栈日志😀\n" * 10000 + f"{fence}\n最终修复建议"
    body = build_failure_comment_body(
        case.config.repositories[0], result, redactor=redactor, diagnosis_report=report,
    )
    pattern = r"<details(?: open)?>.*?</details>"
    before = re.findall(pattern, original, flags=re.DOTALL)
    after = re.findall(pattern, body, flags=re.DOTALL)
    assert after[:-1] == before
    assert "最后一条失败信息" in body
    assert "diagnosis-test-secret" not in body
    assert "&lt;" in body
    assert len(body.encode("utf-8")) <= 54 * 1024
    assert body.count("<details>") == 2
    assert body.count("</details>") == 3
    assert after[-1].startswith("<details>\n<summary>CI 失败诊断报告</summary>\n\n## 失败原因")
    assert f"\n{fence}\n\n> 诊断报告超过评论长度限制" in after[-1]
    assert "最终修复建议" not in body
    assert body.endswith("本地 CI 通过后会自动删除活动失败评论。")


def test_report_fold_tags_cannot_close_outer_fold(diagnosis_case):
    """报告自带折叠标签和未闭合代码块时，外层诊断区仍默认折叠且可正常结束。"""

    report = "## 报告\n\n</details><DETAILS open><summary>内部</summary>\n\n```text\n错误"
    body = build_failure_comment_body(
        diagnosis_case.config.repositories[0], diagnosis_case.result,
        redactor=SecretRedactor(()), diagnosis_report=report,
    )
    assert body.count("<details>") == body.count("</details>") == 2
    assert "<details open>" not in body
    assert html.escape("</details><DETAILS open><summary>内部</summary>") in body
    assert "错误\n```\n\n</details>" in body


async def test_cancelled_diagnosis_keeps_ci_result(diagnosis_case):
    """服务取消需要保留诊断终态并继续传播取消。"""

    case = diagnosis_case
    case.executor.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await diagnose(case)
    assert case.store.get_preflight_diagnosis(case.result.run_id)["status"] == "cancelled"
    assert case.store.get_preflight_run(case.result.run_id)["status"] == "failure"


def test_atomic_claim_and_restart_recovery(diagnosis_case):
    """多线程原子占位与服务重启恢复不会重复创建或覆盖中断状态。"""

    case = diagnosis_case
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: case.store.begin_preflight_diagnosis(case.result.run_id, CI_DIAGNOSIS_AGENT), range(8)))
    assert sum(claims) == 1
    case.store.recover_interrupted_work()
    case.store.finish_preflight_diagnosis(case.result.run_id, status="completed", report="迟到报告")
    record = case.store.get_preflight_diagnosis(case.result.run_id)
    assert record["status"] == "failed"
    assert "异常退出" in record["error"]
    assert record["report"] == ""


@pytest.mark.parametrize("provider", ["github-main", "gitlab-main"])
async def test_report_updates_existing_comment_and_never_revives_stale_failure(diagnosis_case, monkeypatch, provider):
    """两个平台补充折叠报告并保留原始信息，无后台链接，成功后拒绝迟到写回。"""

    case = diagnosis_case
    case.config.repositories[0].provider = provider
    case.config.repositories[0].preflight.publish_failure_comment = True
    case.config.web.public_url = "http://10.151.92.100:8080"
    case.result = case.result.model_copy(update={"error": "测试断言失败"})
    case.store.finish_preflight_run(case.result)
    attachment = "https://gitlab.example.com/-/project/23/uploads/abc/ci.log" if provider == "gitlab-main" else None
    if attachment:
        case.store.save_preflight_log_upload(case.result.run_id, 1, status="completed", attempts=1, url=attachment)
    monkeypatch.setenv("GITLAB_TOKEN", "test-gitlab-secret")
    remote = SimpleNamespace(
        create_change_request_comment=AsyncMock(return_value="comment-1"),
        update_change_request_comment=AsyncMock(return_value=True),
        delete_change_request_comment=AsyncMock(),
    )

    class ProviderContext:
        """模拟平台上下文，不发起任何真实远端请求。"""

        async def __aenter__(self):
            return remote

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr("teamwork_review_agents.managed_comments.create_provider", lambda *_a, **_kw: ProviderContext())
    service = ManagedCommentService(case.config, case.store)
    args = dict(repository_id="demo", number=7, namespace="preflight", slot="teamwork/local-ci",
                source_generation=1, source_head_sha=case.result.head_sha, replace_existing=False)
    await PreflightExecutor(case.config, case.store)._sync_failure_comment(case.config.repositories[0], case.result)
    original = remote.create_change_request_comment.call_args.args[2]
    # 模拟调度只持有精简结果，原始失败信息仍须从持久化记录取回。
    case.result = case.result.model_copy(update={"output": "", "error": None})
    assert await diagnose(case)
    remote.update_change_request_comment.assert_awaited_once()
    remote.create_change_request_comment.assert_awaited_once()
    body = remote.update_change_request_comment.call_args.args[2]
    report_fold = re.search(r"\n<details>\n<summary>CI 失败诊断报告</summary>.*?</details>", body, re.DOTALL)
    assert report_fold is not None
    assert body.replace(report_fold.group(0) + "\n", "", 1) == original
    assert "## 错误报告\n测试断言失败。" in report_fold.group(0)
    assert "<details>\n<summary>末尾输出</summary>" in body
    assert "- 失败步骤：`test-all`" in body
    assert "- 退出码：`1`" in body
    assert "AssertionError: 预期拒绝，实际成功" in body
    assert "测试断言失败" in body
    assert "#preflight=" not in body
    assert case.config.web.public_url not in body
    assert "查看 CI 日志与完整报告" not in body
    if attachment:
        assert f"[下载完整 CI 运行日志]({attachment})" in body
    assert "诊断报告超过评论长度限制" not in body
    case.create_run("ci-new-success", "success")
    assert (await service.publish_latest(**args, body="迟到报告", update_existing=True, preflight_run_id=case.result.run_id))["action"] == "obsolete"
    new = case.create_run("ci-next-failure")
    changed = case.event.current_snapshot.model_copy(update={"head_sha": "b" * 40})
    case.store.save_snapshot_and_events(changed, detect_events(case.event.current_snapshot, changed))
    assert (await service.publish_latest(**args, body="旧源报告", update_existing=True, preflight_run_id=new.run_id))["action"] == "obsolete"
    remote.update_change_request_comment.assert_awaited_once()


async def test_real_executor_diagnosis_skips_checkout_dependencies_and_links_audit(diagnosis_case, monkeypatch):
    """生产执行器只读分支仍保存正常审计运行，却不准备仓库或执行脚本。"""

    case = diagnosis_case
    case.executor = AgentExecutor(case.config, case.store)
    prepare = AsyncMock(side_effect=AssertionError("诊断不能安装依赖"))
    monkeypatch.setattr("teamwork_review_agents.executor.prepare_agent_workspace", prepare)
    monkeypatch.setattr("teamwork_review_agents.executor.check_runtime_readiness", AsyncMock(side_effect=AssertionError("诊断无需命令沙盒")))

    async def model_run(_self, **kwargs):
        """仅替代模型调用，保留整个执行器状态机。"""

        assert kwargs["context"].preflight_run_id == case.result.run_id
        assert kwargs["process_environment"] == {}
        assert kwargs["agent"].sandbox == "read-only"
        assert kwargs["agent"].allowed_sub_agents == []
        assert "read_ci_log" in kwargs["prompt"]
        assert "完整日志" in kwargs["prompt"]
        return AgentResult(run_id=kwargs["run_id"], root_run_id=kwargs["root_run_id"],
                           agent_name=kwargs["agent_name"], status="completed", final_message="## 失败报告\n证据已核对。")

    monkeypatch.setattr("teamwork_review_agents.codex_model_runner.CodexModelRunner.run", model_run)
    assert await diagnose(case)
    record = case.store.get_preflight_diagnosis(case.result.run_id)
    assert record["status"] == "completed", record
    run = case.store.get_run(record["agent_run_id"])
    assert run["status"] == "completed"
    assert run["workspace_status"] == "not-created"
    assert not run["workspace_path"]
    prepare.assert_not_called()


def diagnosis_context(case):
    """绑定测试用例的真实 CI 记录，工具不能由模型另选目标。"""

    return InvocationContext(config_path=str(case.config.config_path), current_agent=CI_DIAGNOSIS_AGENT,
                             run_id="diagnostic", root_run_id="diagnostic", event=case.event,
                             preflight_run_id=case.result.run_id)


def test_log_search_spans_chunks_and_paginates_original_line_numbers(diagnosis_case):
    """完整日志搜索不能遗漏输出块边界或末尾汇总之外的报错。"""

    case = diagnosis_case
    for chunk in ["准备\nTypeEr", "ror: 第一处\n成功\nTypeError: 第二处\n", "结束"]:
        case.store.append_preflight_log(case.result.run_id, stream="stdout", event_type="output", payload=chunk)
    context = diagnosis_context(case)
    first = execute_ci_diagnosis_tool(case.config, context, "read_ci_log", {"search": "typeerror", "limit": 1}, None)
    assert first["lines"] == [{"line": 2, "text": "TypeError: 第一处"}]
    assert first["has_more"]
    second = execute_ci_diagnosis_tool(case.config, context, "read_ci_log", {"search": "typeerror", "offset": first["next_offset"]}, None)
    assert second["lines"] == [{"line": 4, "text": "TypeError: 第二处"}]
    assert not second["has_more"]


def test_historical_log_fallback_and_result_file_boundaries(diagnosis_case, tmp_path):
    """旧 CI 输出仍可检索，补读大结果只允许当前运行的受管文件。"""

    case = diagnosis_case
    context = diagnosis_context(case)
    result = execute_ci_diagnosis_tool(case.config, context, "read_ci_log", {}, None)
    assert result["lines"][0]["text"].startswith("AssertionError")
    root = tmp_path / "runtime"
    (root / "tool-results").mkdir(parents=True)
    (root / "tool-results" / "result-test.json").write_text("完整中文日志", encoding="utf-8")
    page = execute_ci_diagnosis_tool(case.config, context, "read_ci_tool_result", {"filename": "result-test.json", "limit": 2}, root)
    assert page == {"text": "完整", "next_offset": 2}
    with pytest.raises(ValueError):
        execute_ci_diagnosis_tool(case.config, context, "read_ci_tool_result", {"filename": "../auth.json"}, root)


@pytest.mark.parametrize("name", ["execute_command", "apply_patch", "wait_for_ci", "publish_comment", "invoke_agent"])
def test_diagnosis_rejects_all_writing_and_execution_tools(diagnosis_case, name):
    """即使模型猜出通用工具名称，也不能绕过诊断只读边界。"""

    case = diagnosis_case
    with pytest.raises(PermissionError):
        execute_ci_diagnosis_tool(case.config, diagnosis_context(case), name, {}, None)


def test_source_is_read_from_failure_commit_not_current_checkout(diagnosis_case):
    """真实 Git 对象读取固定失败提交，不受当前分支代码或未提交修改影响。"""

    case = diagnosis_case
    root = case.config.repositories[0].workspace

    def git(*args):
        """仅在临时测试仓库创建两个本地提交。"""

        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()

    git("init")
    git("config", "user.email", "ci-test@example.com")
    git("config", "user.name", "CI 测试")
    (root / "test.ts").write_text("失败提交的原始断言\n", encoding="utf-8")
    git("add", "test.ts")
    git("commit", "-m", "初始测试")
    sha = git("rev-parse", "HEAD")
    (root / "test.ts").write_text("最新分支的新断言\n", encoding="utf-8")
    git("commit", "-am", "修改测试")
    with case.store.connect() as connection:
        connection.execute("UPDATE preflight_runs SET head_sha=?", (sha,))
    case.event = case.event.model_copy(update={"new": case.event.current_snapshot.model_copy(update={"head_sha": sha})})
    context = diagnosis_context(case)
    output = execute_ci_diagnosis_tool(case.config, context, "read_ci_source", {"path": "test.ts"}, None)
    assert output["sha"] == sha
    assert output["lines"][0]["text"] == "失败提交的原始断言"
    assert execute_ci_diagnosis_tool(case.config, context, "read_ci_source", {}, None)["lines"][0]["text"] == "test.ts"
    with pytest.raises(ValueError):
        execute_ci_diagnosis_tool(case.config, context, "read_ci_source", {"path": "../auth.json"}, None)
    assert (root / "test.ts").read_text(encoding="utf-8") == "最新分支的新断言\n"


async def test_end_to_end_model_uses_only_diagnosis_tools_and_redacted_evidence(diagnosis_case, monkeypatch):
    """真实执行器及模型工具循环通过模拟 HTTP 完成报告，全程无沙盒脚本或远端调用。"""

    from teamwork_review_agents.codex_model_client import CodexResponsesClient

    case = diagnosis_case
    case.config.runtime.managed_sandbox.enabled = False
    case.executor = AgentExecutor(case.config, case.store)
    case.store.append_preflight_log(case.result.run_id, stream="stdout", event_type="output",
                                   payload="AssertionError: diagnosis-test-secret\n")
    payloads = []

    async def respond(_self, payload, **_kwargs):
        """模拟模型先读完整错误再返回报告，不读取真实账号或发 HTTP。"""

        payloads.append(payload)
        assert {tool["name"] for tool in payload["tools"]} == {"read_ci_log", "read_ci_source", "read_ci_tool_result"}
        if len(payloads) == 1:
            return {"id": "resp1", "output": [{"type": "function_call", "call_id": "call1", "name": "read_ci_log", "arguments": json.dumps({"search": "AssertionError"})}]}
        outputs = [entry for entry in payload["input"] if entry.get("type") == "function_call_output"]
        assert outputs and "AssertionError" in str(outputs)
        assert "diagnosis-test-secret" not in str(outputs)
        return {"id": "resp2", "output": [{"type": "message", "content": [{"type": "output_text", "text": "## 失败原因\n断言失败；具体根因待源码核对。"}]}]}

    monkeypatch.setattr(CodexResponsesClient, "create_response", respond)
    monkeypatch.setattr("teamwork_review_agents.codex_model_client._codex_client_version", lambda _command: "0.159.2")
    monkeypatch.setattr("teamwork_review_agents.codex_model_runner.SkillProjection", lambda *_args, **_kwargs: pytest.fail("诊断不能投影仓库技能"))
    assert await diagnose(case)
    record = case.store.get_preflight_diagnosis(case.result.run_id)
    assert record["status"] == "completed", record
    assert "具体根因待源码核对" in record["report"]
    assert len(payloads) == 2


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_diagnosis_model_total_deadline_and_cancellation(diagnosis_case, monkeypatch, stop):
    """诊断根 Agent 也有总时限，且支持 UI 取消正在等待的模型。"""

    from teamwork_review_agents.codex_model_client import CodexResponsesClient
    from teamwork_review_agents.codex_model_runner import CodexModelRunner

    case = diagnosis_case
    agent = case.config.agents[CI_DIAGNOSIS_AGENT].model_copy(update={"timeout_seconds": 0.1})
    entered = asyncio.Event()

    async def pending(_self, _payload, **_kwargs):
        """模拟持续等待上游，验证看门狗真正中断请求。"""

        entered.set()
        await asyncio.Event().wait()

    async def cancelled():
        return stop == "cancel" and entered.is_set()

    monkeypatch.setattr(CodexResponsesClient, "create_response", pending)
    monkeypatch.setattr("teamwork_review_agents.codex_model_client._codex_client_version", lambda _command: "0.159.2")
    result = await asyncio.wait_for(CodexModelRunner(case.config).run(
        run_id="diagnostic", root_run_id="diagnostic", parent_run_id=None,
        agent_name=CI_DIAGNOSIS_AGENT, agent=agent, repository=case.config.repositories[0],
        context=diagnosis_context(case), prompt="诊断", process_environment={}, cancel_check=cancelled,
    ), timeout=5)
    assert result.status == ("cancelled" if stop == "cancel" else "timed_out")
