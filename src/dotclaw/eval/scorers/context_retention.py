"""CONTEXT_RETENTION Scorer：校验指定文本 / 消息是否在目标 ContextVersion 中。"""

from __future__ import annotations

from ...runtime.domain.context import (
    ConversationMessagesSlotContent,
    ContextContributionKind,
    RunMessageReferencesSlotContent,
    TextSlotContent,
    ToolDefinitionsSlotContent,
)
from ..models import Expectation
from ..results import AssertionResult
from ...trace.models import RunTrace
from .kinds import ExpectationKind


class ContextRetentionScorer:
    """在指定版本的 ContextVersion Slot 中查找期望文本或消息标识。"""

    KIND = ExpectationKind.CONTEXT_RETENTION

    def score(self, trace: RunTrace, expectation: Expectation) -> AssertionResult:
        """在目标上下文版本中查找期望文本 / 消息标识。"""
        expected = expectation.expected
        if not isinstance(expected, str):
            return AssertionResult(expectation, False, "CONTEXT_RETENTION 期望文本或消息 id 必须是字符串")
        kind = str(expectation.options.get("kind", "text")).lower()
        target = expectation.target
        try:
            version = int(target)
        except (TypeError, ValueError):
            return AssertionResult(
                expectation, False, f"CONTEXT_RETENTION target 必须是上下文版本整数，实际 {target!r}"
            )
        context_version = next(
            (item for item in trace.context_versions if item.version == version), None
        )
        if context_version is None:
            available = [item.version for item in trace.context_versions]
            return AssertionResult(
                expectation, False, f"找不到版本 {version} 的 ContextVersion（现有 {available}）"
            )
        found = False
        for slot in context_version.slots:
            content = slot.content
            if isinstance(content, TextSlotContent):
                if expected in content.text:
                    found = True
            elif isinstance(content, ConversationMessagesSlotContent):
                if kind == "text" and any(expected in message.content for message in content.messages):
                    found = True
                elif kind == "message_id" and any(expected == message.message_id for message in content.messages):
                    found = True
            elif isinstance(content, RunMessageReferencesSlotContent):
                if kind == "message_id" and expected in content.message_ids:
                    found = True
            elif isinstance(content, ToolDefinitionsSlotContent):
                if kind == "text" and any(expected in tool.name for tool in content.tools):
                    found = True
        evidence = f"版本 {version} 查找 {kind}={expected!r} -> {'命中' if found else '未命中'}"
        return AssertionResult(expectation, found, evidence)
