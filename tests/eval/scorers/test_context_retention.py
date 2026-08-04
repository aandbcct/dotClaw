"""CONTEXT_RETENTION Scorer：指定文本 / 消息是否在目标 ContextVersion 中。"""

import asyncio
from dotclaw.eval.models import Expectation
from dotclaw.eval.scorers.context_retention import ContextRetentionScorer
from dotclaw.runtime.domain.context import (
    ContextContributionKind,
    ContextOwner,
    ContextPersistenceMode,
    ContextSlotSnapshot,
    ContextSlotStatus,
    ContextVersion,
    ConversationMessagesSlotContent,
    ConversationSlotMessage,
)
from dotclaw.runtime.domain.events import RunEventType
from dotclaw.runtime.domain.facts import MessageRole, RunStatistics
from ..eval_testkit import _ev, run_case_to_trace, synthetic_trace, tool_case

_SCORER = ContextRetentionScorer()


def _ctx_version(version: int, message_id: str) -> ContextVersion:
    snapshot = ContextSlotSnapshot(
        slot_id="conv",
        owner=ContextOwner.SESSION,
        contribution_kind=ContextContributionKind.CONVERSATION_MESSAGES,
        persistence_mode=ContextPersistenceMode.SNAPSHOT,
        status=ContextSlotStatus.INCLUDED,
        injection_order=0,
        content=ConversationMessagesSlotContent(
            messages=(
                ConversationSlotMessage(message_id=message_id, role=MessageRole.USER, content="hi", created_at="t"),
            )
        ),
        content_hash="h",
    )
    return ContextVersion(version=version, created_at="t", slots=(snapshot,), content_hash="h", tool_schema_hash="h")


async def _real_trace():
    return await run_case_to_trace(tool_case())


async def test_text_pass():
    trace = await _real_trace()
    result = _SCORER.score(trace, Expectation("context_retention", "1", "SECRET_SYSTEM_TEXT", {"kind": "text"}))
    assert result.passed is True


async def test_text_fail():
    trace = await _real_trace()
    result = _SCORER.score(trace, Expectation("context_retention", "1", "NONEXISTENT", {"kind": "text"}))
    assert result.passed is False


async def test_version_not_found_fail():
    trace = await _real_trace()
    result = _SCORER.score(trace, Expectation("context_retention", "99", "x", {"kind": "text"}))
    assert result.passed is False


async def test_message_id_pass():
    trace = synthetic_trace(
        [_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)],
        statistics=RunStatistics(),
        context_versions=(_ctx_version(1, "u1"),),
    )
    result = _SCORER.score(trace, Expectation("context_retention", "1", "u1", {"kind": "message_id"}))
    assert result.passed is True


async def test_message_id_fail():
    trace = synthetic_trace(
        [_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)],
        statistics=RunStatistics(),
        context_versions=(_ctx_version(1, "u1"),),
    )
    result = _SCORER.score(trace, Expectation("context_retention", "1", "nope", {"kind": "message_id"}))
    assert result.passed is False
