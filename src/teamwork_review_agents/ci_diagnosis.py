"""自动 CI 失败后的独立诊断，不改变 CI 门禁结论。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from .config import AppConfig
from .environment import SecretRedactor, resolve_environment, resolve_provider_token
from .executor import AgentExecutor
from .managed_comments import ManagedCommentService
from .models import ChangeEvent, PreflightResult, stable_hash
from .preflight import build_failure_comment_body
from .state import RunReservation, StateStore


async def diagnose_ci_failure(
    config: AppConfig, store: StateStore, executor: AgentExecutor,
    event: ChangeEvent, result: PreflightResult,
) -> bool:
    """每条 CI 最多诊断一次，缺失 Agent 作为可见的跳过记录保存。"""

    repository = config.repository_map()[event.repository_id]
    settings = repository.preflight
    if not settings.failure_diagnosis_enabled:
        return False
    run = await asyncio.to_thread(store.get_preflight_run, result.run_id)
    if run is None or run["trigger_source"] != "event":
        return False
    if run["status"] not in {"failure", "timed_out", "error"}:
        return False
    if run["status"] == "error" and run["attempts"] < config.runtime.event_retry_count + 1:
        return False
    agent_name = settings.failure_diagnosis_agent
    missing = agent_name not in config.agents
    reason = f"指定的诊断 Agent 不存在或已删除：{agent_name or '未指定'}；已跳过失败诊断" if missing else None
    claimed = await asyncio.to_thread(store.begin_preflight_diagnosis, result.run_id, agent_name, skip_reason=reason)
    if not claimed or missing:
        return False

    # 复用 CI 时仍按原始记录绑定源码，不受后续 MR 分支移动影响。
    original = await asyncio.to_thread(store.load_event, run["event_id"]) if run["event_id"] else None
    selected_event = original or event
    selected_event = selected_event.model_copy(update={
        "current": selected_event.current_snapshot.model_copy(update={"head_sha": run["head_sha"]}),
    })
    environment = resolve_environment(config, repository, config.agents[agent_name], selected_event, result.run_id)
    token = resolve_provider_token(config, config.providers[repository.provider], repository)
    redactor = SecretRedactor((*environment.secret_values, token))
    context: dict[str, Any] = {
        "preflight_run_id": result.run_id, "repository": repository.id,
        "number": run["number"], "url": selected_event.current_snapshot.web_url,
        "head_sha": run["head_sha"], "status": run["status"],
        "failed_step": run["failed_step"], "exit_code": run["exit_code"], "error": run["error"],
        "steps": run["steps"],
        "started_at": datetime.fromtimestamp(run["started_at"]).astimezone().isoformat(),
        "finished_at": datetime.fromtimestamp(run["finished_at"]).astimezone().isoformat() if run["finished_at"] else None,
    }
    agent_run_id: str | None = None

    async def started(reservation: RunReservation) -> None:
        """模型启动前立即关联运行，使排队和取消状态可追踪。"""

        nonlocal agent_run_id
        agent_run_id = reservation.run_id
        await asyncio.to_thread(store.link_preflight_diagnosis_run, result.run_id, reservation.run_id)

    try:
        answer = await executor.execute(
            agent_name=agent_name, event=selected_event,
            idempotency_key=stable_hash("ci-diagnosis", result.run_id),
            rule_name="CI 失败诊断", task="根据本次 CI 日志与固定提交源码生成 Markdown 失败报告",
            extra_context=redactor.data(context), preflight_run_id=result.run_id,
            run_started_callback=started,
        )
        if answer is None or not answer.final_message.strip():
            raise RuntimeError("诊断 Agent 未返回有效报告")
        report = redactor.text(answer.final_message.strip())
        await asyncio.to_thread(store.finish_preflight_diagnosis, result.run_id, status="completed", report=report)
    except asyncio.CancelledError:
        await asyncio.to_thread(store.finish_preflight_diagnosis, result.run_id, status="cancelled", error="CI 诊断已中断")
        raise
    except Exception as exc:
        agent_run = await asyncio.to_thread(store.get_run, agent_run_id) if agent_run_id else None
        status = agent_run.get("status") if agent_run else "failed"
        await asyncio.to_thread(
            store.finish_preflight_diagnosis, result.run_id,
            status=status if status in {"cancelled", "timed_out"} else "failed",
            error=redactor.text(str(exc)),
        )
        return True

    if settings.publish_failure_comment and run["number"] is not None:
        try:
            # 从持久化的同次 CI 重建原始摘要和日志，复用结果也不能丢失失败证据。
            body = build_failure_comment_body(
                repository, PreflightResult.model_validate(run),
                redactor=redactor, diagnosis_report=report,
                log_url=(run["log_upload"]["url"] if run.get("log_upload")
                         and run["log_upload"]["status"] == "completed" else None),
            )
            await ManagedCommentService(config, store).publish_latest(
                repository_id=repository.id, number=run["number"], namespace="preflight",
                slot=settings.status_context, source_generation=selected_event.source_generation,
                source_head_sha=run["head_sha"], body=body,
                replace_existing=False, update_existing=True, preflight_run_id=result.run_id,
            )
        except Exception as exc:
            await asyncio.to_thread(store.set_preflight_diagnosis_comment_error, result.run_id, redactor.text(str(exc)))
    return True
