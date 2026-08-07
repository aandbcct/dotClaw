"""PR3 并发正确性断言：顺序、归属、隔离、取消与可比性判定。

本模块全部为纯函数，基于已读取的 ``RunFacts`` 做判定，不依赖 Runtime 执行。
每条断言返回 ``AssertionResult``（通过/失败 + 证据），不抛异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.facts import MessageRole, RunMessageKind

from .concurrency_workloads import IdentifierCodec, RunFacts


# --------------------------------------------------------------------------- #
# 断言结果
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AssertionResult:
    """单条断言的判定结果。"""

    label: str
    """断言标签（如 "fifo_order"）。"""

    passed: bool
    """是否通过。"""

    detail: str = ""
    """通过时为空或简要说明；失败时记录具体原因。"""

    evidence: dict[str, object] = field(default_factory=dict)
    """判定证据（如乱序对、串扰内容）。"""


def _pass(label: str, detail: str = "") -> AssertionResult:
    """构造一条通过的断言。"""
    return AssertionResult(label=label, passed=True, detail=detail)


def _fail(label: str, detail: str, **evidence: object) -> AssertionResult:
    """构造一条失败的断言。"""
    return AssertionResult(label=label, passed=False, detail=detail, evidence=dict(evidence))


# --------------------------------------------------------------------------- #
# FIFO 顺序断言
# --------------------------------------------------------------------------- #


def assert_fifo_order(
    facts_list: Sequence[RunFacts],
    accepted_seqs: Sequence[int],
) -> list[AssertionResult]:
    """验证同 Session Run 的执行/完成/Conversation 顺序均等于 accepted_seq。

    参数：
        facts_list: 已按 accepted_seq 排序的 RunFacts 列表。
        accepted_seqs: 对应的 accepted_seq 列表（1-based）。
    """
    results: list[AssertionResult] = []
    n: int = len(facts_list)

    # 基本计数检查
    if n != len(accepted_seqs):
        results.append(_fail("fifo_count", f"Run 数 {n} ≠ accepted_seq 数 {len(accepted_seqs)}"))
        return results

    if n == 0:
        results.append(_fail("fifo_count", "没有 Run 事实"))
        return results

    # 检查是否所有 Run 都完成
    incomplete: list[str] = [
        facts.run_id for facts in facts_list if facts.state_outcome != "completed"
    ]
    if incomplete:
        results.append(
            _fail("fifo_completion", f"{len(incomplete)} 个 Run 未完成", incomplete_runs=incomplete)
        )
        return results

    # 提取执行开始序号（按第一个 run_started 事件时间戳排序）
    started_order: list[int] = _execution_started_order(facts_list, accepted_seqs)

    # 提取完成序号（按 run_completed 事件时间戳排序）
    completed_order: list[int] = _completed_order(facts_list, accepted_seqs)

    # 检查乱序
    expected: list[int] = list(range(1, n + 1))
    # 实际 found seqs
    found_seqs: set[int] = set(accepted_seqs)

    # 重复/遗漏检查
    if len(found_seqs) != n:
        duplicates: dict[int, int] = {}
        for s in accepted_seqs:
            duplicates[s] = duplicates.get(s, 0) + 1
        dup_entries = {k: v for k, v in duplicates.items() if v > 1}
        missing = set(range(1, n + 1)) - found_seqs
        results.append(
            _fail("fifo_duplicates_or_missing",
                  f"重复: {dup_entries}, 遗漏: {sorted(missing)}",
                  duplicates=dup_entries, missing=sorted(missing))
        )
        return results

    # 乱序检查：执行开始顺序（仅在事件可用时检查）
    if started_order and started_order != expected:
        results.append(
            _fail("fifo_execution_order",
                  f"执行开始顺序 {started_order} ≠ 预期 {expected}",
                  actual=started_order, expected=expected)
        )

    # 乱序检查：完成顺序（仅在事件可用时检查）
    if completed_order and completed_order != expected:
        results.append(
            _fail("fifo_completion_order",
                  f"完成顺序 {completed_order} ≠ 预期 {expected}",
                  actual=completed_order, expected=expected)
        )

    # Conversation 顺序检查
    conv_order: list[int] = _conversation_order(facts_list, accepted_seqs)
    if conv_order and conv_order != expected:
        results.append(
            _fail("fifo_conversation_order",
                  f"Conversation 顺序 {conv_order} ≠ 预期 {expected}",
                  actual=conv_order, expected=expected)
        )

    if not results:
        results.append(_pass("fifo_order", f"全部 {n} 个 Run 顺序一致"))

    return results


def _execution_started_order(
    facts_list: Sequence[RunFacts],
    accepted_seqs: Sequence[int],
) -> list[int]:
    """按 run_started 事件出现时间排序，返回对应的 accepted_seq 列表。"""
    pairs: list[tuple[str, int]] = []
    for facts, seq in zip(facts_list, accepted_seqs):
        for event in facts.events:
            if event.event_type == RunEventType.RUN_STARTED:
                pairs.append((event.occurred_at, seq))
                break
    # 按时间戳排序
    pairs.sort(key=lambda x: x[0])
    return [seq for _, seq in pairs]


def _completed_order(
    facts_list: Sequence[RunFacts],
    accepted_seqs: Sequence[int],
) -> list[int]:
    """按 run_completed 事件出现时间排序，返回对应的 accepted_seq 列表。"""
    pairs: list[tuple[str, int]] = []
    for facts, seq in zip(facts_list, accepted_seqs):
        for event in facts.events:
            if event.event_type == RunEventType.RUN_COMPLETED:
                pairs.append((event.occurred_at, seq))
                break
    # 按时间戳排序
    pairs.sort(key=lambda x: x[0])
    return [seq for _, seq in pairs]


def _conversation_order(
    facts_list: Sequence[RunFacts],
    accepted_seqs: Sequence[int],
) -> list[int]:
    """按最终回答在 messages 中的位置排序，返回对应的 accepted_seq 列表。

    通过比较 RunFacts 中消息的时间戳来确定 Conversation 持久化顺序。
    若所有消息均无时间戳，则返回空列表（跳过该检查）。
    """
    pairs: list[tuple[int, int]] = []  # (message_sequence, accepted_seq)
    for facts, seq in zip(facts_list, accepted_seqs):
        # 使用最后一条 assistant 消息的 sequence 作为完成顺序近似
        for msg in reversed(facts.messages):
            if msg.role == MessageRole.ASSISTANT and msg.content:
                pairs.append((msg.sequence, seq))
                break
    if not pairs:
        return []
    pairs.sort(key=lambda x: x[0])
    return [seq for _, seq in pairs]


# --------------------------------------------------------------------------- #
# 多 Session 隔离断言
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IsolationResult:
    """一次多 Session 隔离检查的完整结果。"""

    total_requests: int
    """总请求数。"""

    message_leak_count: int
    """跨 Session 消息串扰数。"""

    event_leak_count: int
    """跨 Session 事件串扰数。"""

    context_leak_count: int
    """跨 Session 上下文串扰数。"""

    tool_leak_count: int
    """跨 Session 工具结果串扰数。"""

    stream_leak_count: int
    """跨 Session 输出串流数。"""

    details: list[str] = field(default_factory=list)
    """串扰详情列表。"""

    @property
    def any_leak(self) -> bool:
        """是否存在任何串扰。"""
        return (
            self.message_leak_count > 0
            or self.event_leak_count > 0
            or self.context_leak_count > 0
            or self.tool_leak_count > 0
            or self.stream_leak_count > 0
        )


def check_isolation(
    facts_by_session: dict[int, list[RunFacts]],
    session_count: int,
) -> IsolationResult:
    """检查跨 Session 的运行事实隔离性。

    参数：
        facts_by_session: session_index -> 该 Session 全部 RunFacts。
        session_count: 实验中的 Session 总数。
    """
    total_requests: int = sum(len(v) for v in facts_by_session.values())

    counts: dict[str, int] = {
        "message": 0, "event": 0, "context": 0, "tool": 0, "stream": 0,
    }
    details: list[str] = []

    def count_foreign(contents: Sequence[str], session_index: int, category: str, run_id: str) -> None:
        """从一类实际事实中累计外部 Session 标识，缺失事实同样判为不可验收。"""
        if not contents:
            counts[category] += 1
            details.append(f"{category} 证据缺失: Session {session_index} Run {run_id}")
            return
        for content in contents:
            foreign: set[int] = IdentifierCodec.extract_session_indices(content) - {session_index}
            if foreign:
                counts[category] += len(foreign)
                details.append(
                    f"{category} 串扰: Session {session_index} Run {run_id} 包含 Session {sorted(foreign)} 标识"
                )

    for si in range(session_count):
        own_facts: list[RunFacts] = facts_by_session.get(si, [])
        for facts in own_facts:
            count_foreign([message.content for message in facts.messages if message.content], si, "message", facts.run_id)

            # RunEvent 不含 Session 字段，因此以持久化读取路径、run_id 与消息引用三者校验归属。
            event_message_ids = {message.message_id for message in facts.messages}
            invalid_events = [
                event for event in facts.events
                if event.run_id != facts.run_id
                or any(message_id not in event_message_ids for message_id in event.message_ids)
            ]
            if not facts.events:
                counts["event"] += 1
                details.append(f"event 证据缺失: Session {si} Run {facts.run_id}")
            elif invalid_events:
                counts["event"] += len(invalid_events)
                details.append(f"event 归属异常: Session {si} Run {facts.run_id}")

            context_contents = [str(version) for version in facts.context_versions]
            count_foreign(context_contents, si, "context", facts.run_id)
            requested_tool_call_ids = {
                call.call_id
                for message in facts.messages
                if message.kind is RunMessageKind.LLM_RESPONSE
                for call in message.tool_calls
            }
            invalid_tool_records = [
                record for record in facts.tool_records
                if record.tool_call_id is None or record.tool_call_id not in requested_tool_call_ids
            ]
            if not facts.tool_records:
                counts["tool"] += 1
                details.append(f"tool 持久化记录缺失: Session {si} Run {facts.run_id}")
            elif invalid_tool_records:
                counts["tool"] += len(invalid_tool_records)
                details.append(f"tool 归属异常: Session {si} Run {facts.run_id}")
            else:
                count_foreign([record.content for record in facts.tool_records], si, "tool", facts.run_id)
            count_foreign(list(facts.stream_contents), si, "stream", facts.run_id)

    return IsolationResult(
        total_requests=total_requests,
        message_leak_count=counts["message"],
        event_leak_count=counts["event"],
        context_leak_count=counts["context"],
        tool_leak_count=counts["tool"],
        stream_leak_count=counts["stream"],
        details=details,
    )


def assert_isolation(isolation: IsolationResult) -> list[AssertionResult]:
    """将 IsolationResult 转为断言结果列表。"""
    results: list[AssertionResult] = []

    if isolation.message_leak_count > 0:
        results.append(_fail("isolation_messages",
                             f"跨 Session 消息串扰: {isolation.message_leak_count}",
                             count=isolation.message_leak_count, details=isolation.details))
    else:
        results.append(_pass("isolation_messages", "消息隔离通过"))

    if isolation.event_leak_count > 0:
        results.append(_fail("isolation_events",
                             f"跨 Session 事件串扰: {isolation.event_leak_count}",
                             count=isolation.event_leak_count, details=isolation.details))
    else:
        results.append(_pass("isolation_events", "事件隔离通过"))

    if isolation.context_leak_count > 0:
        results.append(_fail("isolation_context",
                             f"跨 Session 上下文串扰: {isolation.context_leak_count}",
                             count=isolation.context_leak_count))
    else:
        results.append(_pass("isolation_context", "上下文隔离通过"))

    if isolation.tool_leak_count > 0:
        results.append(_fail("isolation_tools",
                             f"跨 Session 工具串扰: {isolation.tool_leak_count}",
                             count=isolation.tool_leak_count))
    else:
        results.append(_pass("isolation_tools", "工具隔离通过"))

    if isolation.stream_leak_count > 0:
        results.append(_fail("isolation_streams",
                             f"跨 Session 输出串流: {isolation.stream_leak_count}",
                             count=isolation.stream_leak_count))
    else:
        results.append(_pass("isolation_streams", "输出串流隔离通过"))

    if not isolation.any_leak:
        results.append(_pass("isolation_overall",
                             f"跨 {isolation.total_requests} 请求零串扰"))

    return results


# --------------------------------------------------------------------------- #
# 取消断言
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CancelResult:
    """一次取消实验的判定结果。"""

    delivered: bool
    """取消信号是否已送达（cancel() 返回成功）。"""

    effective: bool
    """取消是否已生效（Run 进入取消终态）。"""

    lock_released: bool
    """取消后 Session 锁是否已释放。"""

    followup_completed: bool
    """取消后同 Session 后续请求是否完成。"""

    delivery_ms: float | None = None
    """取消送达耗时（毫秒）。"""

    effect_ms: float | None = None
    """取消生效耗时（毫秒）。"""


def assert_cancel(cancel_result: CancelResult) -> list[AssertionResult]:
    """将 CancelResult 转为断言结果列表。"""
    results: list[AssertionResult] = []

    if cancel_result.delivered:
        results.append(_pass("cancel_delivered",
                             f"送达耗时: {cancel_result.delivery_ms:.1f} ms" if cancel_result.delivery_ms else "已送达"))
    else:
        results.append(_fail("cancel_delivered", "取消信号未送达"))

    if cancel_result.effective:
        results.append(_pass("cancel_effective",
                             f"生效耗时: {cancel_result.effect_ms:.1f} ms" if cancel_result.effect_ms else "已生效"))
    else:
        results.append(_fail("cancel_effective", "取消未生效（Run 未进入取消终态）"))

    if cancel_result.lock_released:
        results.append(_pass("cancel_lock_released", "Session 锁已释放"))
    else:
        results.append(_fail("cancel_lock_released", "取消后 Session 锁未释放"))

    if cancel_result.followup_completed:
        results.append(_pass("cancel_followup", "后续同 Session 请求完成"))
    else:
        results.append(_fail("cancel_followup", "取消后同 Session 后续请求未完成"))

    return results


# --------------------------------------------------------------------------- #
# 可比性断言
# --------------------------------------------------------------------------- #


def assert_comparable_configs(
    session_config: dict[str, object],
    global_config: dict[str, object],
) -> list[AssertionResult]:
    """验证两种调度模式使用相同负载配置（可比较的前置条件）。"""
    results: list[AssertionResult] = []

    shared_keys: set[str] = {
        "session_count", "requests_per_session", "fake_delay_ms", "warmup", "repeat"
    }

    for key in sorted(shared_keys):
        sv = session_config.get(key)
        gv = global_config.get(key)
        if sv != gv:
            results.append(
                _fail("comparable_config",
                      f"配置 {key} 不一致: session={sv}, global={gv}",
                      key=key, session_value=sv, global_value=gv)
            )

    if not results:
        results.append(_pass("comparable_config", "两种调度模式配置一致"))

    return results
