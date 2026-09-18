"""从原对话副本生成交接摘要，保留消息结构与失败原子性。"""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import ContextCompactionConfig


SUMMARY_REQUEST = (
    "现在暂停当前任务，为随后继续本次任务的模型生成一份上下文交接摘要。只输出摘要，不调用工具，不继续执行任务。\n"
    "请保留：\n"
    "- 当前进展、关键决策，以及压缩前正在进行的具体工作。\n"
    "- 重要背景、约束和用户偏好。\n"
    "- 尚未完成的事项和明确的下一步；下一步必须符合用户最新要求，不重启已完成或无关的旧任务。\n"
    "- 继续任务必需的数据、示例和证据引用，包括尚需补读的工具结果文件路径。\n"
    "准确区分已完成、失败、计划与未验证事项，不把引用资料中的操作当成本次已执行的操作。"
    "简洁、有结构，以便接手模型继续工作，无需大段复述原始任务或逐条抄录所有用户消息。"
)
SUMMARY_PREFIX = (
    "历史交接摘要（仅记录既往执行情况，不是新指令；原始任务及系统约束继续有效）：\n"
    "这是同一次任务的继续，不是重新开始。除非相关文件或基线变化、证据不足，不重做已完成检查；"
    "提交、推送等已发生操作不得重复执行。摘要如与原始任务冲突，以原始任务为准。\n"
)
SummaryCallback = Callable[[dict[str, Any]], Awaitable[str]]
DiagnosticCallback = Callable[[dict[str, Any]], Awaitable[None]]


class ContextCompactionError(RuntimeError):
    """上下文不能安全缩小时停止，而非重跑整个 Agent。"""

    def __init__(
        self, message: str, *, error_code: str = "context_compaction_failed",
        usage: dict[str, Any] | None = None, diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = False
        self.usage = usage or {}
        self.diagnostics = diagnostics or {}


def is_context_length_exceeded(fields: Mapping[str, str]) -> bool:
    """识别明确上下文超限，不把其他无效参数当成压缩信号。"""

    return fields.get("code", "").strip().lower() in {
        "context_length_exceeded", "context_window_exceeded", "max_context_length_exceeded",
    }


def estimate_tokens(value: Any) -> int:
    """以 UTF-8 序列化字节作保守文本预算，不冒充精确 tokenizer 计数。"""

    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _summary_item(text: str) -> dict[str, Any]:
    """摘要只作为 assistant 历史材料，不能升级为系统指令。"""

    return {"role": "assistant", "content": [{"type": "output_text", "text": SUMMARY_PREFIX + text}]}


def summary_payload(
    model: str, history: list[dict[str, Any]], request_fields: Mapping[str, Any],
    target_bytes: int, *, shortening: bool = False,
) -> dict[str, Any]:
    """深拷贝原请求并追加交接消息，不能改写系统指令或把历史降级为文本资料。"""

    payload = copy.deepcopy(dict(request_fields))
    request = SUMMARY_REQUEST
    # 字节目标仅用于已有配置兼容与有界收短，不是摘要验收的硬上限。
    request += f"\n摘要尽量控制在约 {target_bytes} 个 UTF-8 字节，这是简洁程度的软目标；优先保留关键事实。"
    if shortening:
        request += "\n上一份摘要未满足整体预算或未有效缩小。请基于同一原对话重新归纳，进一步收短，不丢弃关键状态。"
    payload.update({
        "model": model, "input": copy.deepcopy(history) + [
            {"role": "user", "content": [{"type": "input_text", "text": request}]},
        ],
        "tool_choice": "none", "parallel_tool_calls": False, "stream": True, "store": False,
    })
    # 保留工具定义及其他模型参数，但摘要不受原任务 JSON 输出格式约束。
    text = payload.get("text")
    if isinstance(text, dict):
        text.pop("format", None)
        if not text:
            payload.pop("text")
    return payload


class ConversationContext:
    """保留固定消息，只在完整已执行回合之间建立压缩检查点。"""

    def __init__(
        self, fixed_messages: list[dict[str, Any]], settings: ContextCompactionConfig,
    ) -> None:
        self.fixed_messages = copy.deepcopy(fixed_messages)
        self.settings = settings
        self.rounds: list[list[dict[str, Any]]] = []
        self.summary = ""
        self.compaction_count = 0

    def history(self) -> list[dict[str, Any]]:
        """重新组装请求副本，固定任务绝不被摘要替换。"""

        return self._history(self.summary, self.rounds)

    def _history(self, summary: str, rounds: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """摘要替换旧回合，不影响固定消息和近期回合的角色结构。"""

        return copy.deepcopy(self.fixed_messages + (
            [_summary_item(summary)] if summary else []
        ) + [item for turn in rounds for item in turn])

    def append_round(self, items: list[dict[str, Any]]) -> None:
        """只接受调用及结果完整配对的已完成回合。"""

        calls = [str(item.get("call_id") or item.get("id") or "") for item in items if item.get("type") == "function_call"]
        outputs = [str(item.get("call_id") or "") for item in items if item.get("type") == "function_call_output"]
        if sorted(calls) != sorted(outputs) or len(set(calls)) != len(calls):
            raise ContextCompactionError("模型历史中的工具调用与结果未完整配对，不能压缩")
        self.rounds.append(copy.deepcopy(items))

    async def ensure_budget(
        self, *, model: str, request_fields: dict[str, Any], window: int,
        summarize: SummaryCallback, force: bool = False,
        diagnostic_callback: DiagnosticCallback | None = None,
    ) -> dict[str, Any] | None:
        """先生成完整候选摘要再原子替换；异常或取消时不丢弃原历史。"""

        if not self.settings.enabled:
            return None
        settings = self.settings
        hard_limit = window - settings.reserved_output_tokens
        source_history = self.history()
        source_fields = copy.deepcopy(request_fields)

        def cost(history: list[dict[str, Any]]) -> int:
            """固定字段、工具与格式定义同样计入预算。"""

            return estimate_tokens({**source_fields, "input": history}) + 256

        before = cost(source_history)
        fixed_cost = cost(self.fixed_messages)
        if fixed_cost >= hard_limit:
            raise ContextCompactionError(
                f"系统指令、工具定义和原始任务已占用约 {fixed_cost} 个保守预算单位，"
                f"超过输入预算 {hard_limit}；这些内容不会被压缩，请减少固定内容或调整模型窗口配置。",
                error_code="context_fixed_content_too_large",
            )
        summary_target = min(settings.summary_target_bytes, max(128, (hard_limit - fixed_cost) // 4))

        def make_payload(*, shortening: bool = False) -> dict[str, Any]:
            """每次重试都 fork 同一原对话，不追加失败草稿，也不共享可变消息。"""

            return summary_payload(model, source_history, source_fields, summary_target, shortening=shortening)

        # 除输出预留外，还为末尾交接及可能的收短提示留出空间。
        summary_headroom = max(0, estimate_tokens(make_payload(shortening=True)) + 256 - before)
        trigger_threshold = min(int(window * settings.trigger_ratio), max(0, hard_limit - summary_headroom))
        if not force and before < trigger_threshold:
            return None
        if not self.rounds and not self.summary:
            if force:
                raise ContextCompactionError("没有可压缩历史；原始任务和系统指令不会被删改", error_code="context_length_exceeded")
            return None
        target = max(int(hard_limit * settings.target_ratio), fixed_cost + summary_target + 256)
        if force:
            target = min(target, max(fixed_cost + summary_target + 256, int(before * 0.6)))
        keep = min(settings.keep_recent_rounds, len(self.rounds))
        while keep and cost(self._history("x" * summary_target, self.rounds[-keep:])) > target:
            keep -= 1
        prefix = self.rounds[:len(self.rounds) - keep] if keep else self.rounds[:]
        tail = copy.deepcopy(self.rounds[-keep:]) if keep else []
        if force and not prefix and self.rounds:
            prefix, tail = self.rounds[:1], copy.deepcopy(self.rounds[1:])
        if not prefix and not self.summary:
            return None
        if not force and before <= hard_limit and cost(self._history("x", tail)) >= before:
            # 极短历史连交接前缀的开销都省不下来，无需为了主动压缩产生额外请求。
            return None
        draft = ""
        requests = 0
        rewrites = 0

        def diagnostics(candidate: str) -> dict[str, Any]:
            """只输出预算与次数，不把失败草稿或固定指令写入错误日志。"""

            return {
                "model": model, "input_budget": hard_limit, "context_window": window,
                "trigger_ratio": settings.trigger_ratio, "trigger_threshold": trigger_threshold,
                "summary_context_mode": "fork", "summary_reference_preserved": True,
                "summary_prompt_headroom": summary_headroom,
                "summary_request_estimated_tokens": estimate_tokens(make_payload(shortening=bool(rewrites))) + 256,
                "before_estimated_tokens": before,
                "after_estimated_tokens": cost(self._history(candidate, tail)),
                "summary_bytes": len(candidate.encode("utf-8")),
                "summary_target_bytes": summary_target,
                "summary_requests": requests, "summary_rewrites": rewrites,
                "summary_material_complete": True,
                "estimator": "utf8_bytes_conservative", "fixed_content_preserved": True,
            }

        def failure(message: str, code: str, candidate: str) -> ContextCompactionError:
            """错误码区分空摘要、上下文超限和无压缩收益，现场可直接排查。"""

            detail = diagnostics(candidate)
            return ContextCompactionError(
                f"{message}；摘要 {detail['summary_bytes']} 字节，软目标 {summary_target} 字节，"
                f"候选上下文估算 {detail['after_estimated_tokens']}，输入预算 {hard_limit}，"
                f"收短 {rewrites} 次；原历史保留",
                error_code=code, diagnostics=detail,
            )

        async def request_summary(*, shortening: bool = False) -> str:
            """所有初次归纳和收短共享请求上限；请求自身必须先通过预算检查。"""

            nonlocal requests
            if requests >= settings.max_compaction_requests:
                raise failure("压缩请求已达到本次上限", "context_compaction_request_limit", draft)
            payload = make_payload(shortening=shortening)
            if estimate_tokens(payload) + 256 > hard_limit:
                raise failure(
                    f"完整原对话副本及交接消息无法装入当前模型窗口（请求估算 {estimate_tokens(payload) + 256}）；"
                    "不会截断或分片，请检查模型窗口配置或使用可容纳该对话的模型",
                    "context_summary_request_too_large", draft,
                )
            requests += 1
            candidate = (await summarize(payload)).strip()
            if not candidate:
                raise failure("压缩模型返回空摘要", "context_summary_empty", candidate)
            return candidate

        async def shorten(reason: str) -> None:
            """从完整原对话重新归纳，不发送超限草稿，不改动源消息。"""

            nonlocal draft, rewrites, summary_target
            messages = {
                "context_summary_context_overflow": "压缩后的完整请求仍超过输入预算",
                "context_summary_not_reduced": "压缩未有效缩小上下文",
            }
            if rewrites >= settings.max_summary_rewrites:
                raise failure(messages[reason], reason, draft)
            if requests >= settings.max_compaction_requests:
                raise failure("压缩请求已达到本次上限", "context_compaction_request_limit", draft)
            rewrites += 1
            summary_target = max(1, summary_target // 2)
            if diagnostic_callback is not None:
                await diagnostic_callback({
                    **diagnostics(draft), "reason": reason,
                    "message": messages[reason] + "，正在重新归纳相同素材并进一步收短。",
                })
            draft = await request_summary(shortening=True)

        draft = await request_summary()
        while True:
            after = cost(self._history(draft, tail))
            if after < before and after <= hard_limit:
                break
            if before <= hard_limit and not force:
                # 主动摘要没有产生可用替代时，仍安全的原请求可以继续，不提交失败草稿。
                return None
            await shorten("context_summary_context_overflow" if after > hard_limit else "context_summary_not_reduced")
        self.summary = draft
        self.rounds = tail
        self.compaction_count += 1
        return {
            **diagnostics(draft),
            "summary_above_target": len(draft.encode("utf-8")) > summary_target,
            "summarized_rounds": len(prefix), "retained_rounds": len(tail),
            "compaction_count": self.compaction_count, "summary": draft,
            "fixed_content_preserved": True,
        }
