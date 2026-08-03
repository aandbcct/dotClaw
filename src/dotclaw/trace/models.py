"""RunTrace 领域模型。

基于 Runtime v4 权威事实（``AgentRun`` / ``RunEvent`` / ``RunMessage`` /
``ContextVersion``）重建的只读追踪模型。所有类型均为不可变 dataclass 或固定
``StrEnum``，不引入 Pydantic、不派生子类 Span、不建立开放注册表。

``TraceSpan`` 只保存消息 ID 与 Context Version 引用，禁止内联消息正文、工具完整
输出或事件副本；结构化重建证据统一表达为 ``TraceIssue``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping

from ..runtime.domain.context import ContextVersion
from ..runtime.domain.facts import AgentRun, RunMessage


class SpanKind(StrEnum):
    """追踪 Span 的五大类型。"""

    RUN = "run"
    LLM = "llm"
    TOOL = "tool"
    APPROVAL = "approval"
    DELEGATION = "delegation"


class TraceSpanStatus(StrEnum):
    """Span 的重建后状态。"""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING = "waiting"
    INCOMPLETE = "incomplete"


class TraceIssueKind(StrEnum):
    """语义不完整的结构化重建证据类别（非 PR7 根因）。"""

    MISSING_EVENT_PAIR = "missing_event_pair"
    MISSING_MESSAGE = "missing_message"
    MISSING_CONTEXT_VERSION = "missing_context_version"
    UNSUPPORTED_EVENT = "unsupported_event"
    CONFLICTING_REFERENCE = "conflicting_reference"


@dataclass(frozen=True)
class TraceIssue:
    """重建时无法完整配对或发现语义冲突的结构化证据。"""

    kind: TraceIssueKind
    evidence: str
    event_sequence: int | None = None
    message_id: str | None = None
    span_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        """序列化为稳定字典。"""
        return {
            "kind": self.kind.value,
            "evidence": self.evidence,
            "event_sequence": self.event_sequence,
            "message_id": self.message_id,
            "span_id": self.span_id,
        }


@dataclass(frozen=True)
class TraceSpan:
    """一个重建出来的执行区间；只保存消息/上下文引用与已知事实属性。"""

    span_id: str
    kind: SpanKind
    parent_span_id: str | None
    started_at: str
    ended_at: str | None
    status: TraceSpanStatus
    start_event_sequence: int | None
    end_event_sequence: int | None
    message_ids: tuple[str, ...]
    context_version: int | None
    attributes: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        """序列化为稳定字典。"""
        return {
            "span_id": self.span_id,
            "kind": self.kind.value,
            "parent_span_id": self.parent_span_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status.value,
            "start_event_sequence": self.start_event_sequence,
            "end_event_sequence": self.end_event_sequence,
            "message_ids": list(self.message_ids),
            "context_version": self.context_version,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True)
class TraceMetrics:
    """从 Span 派生的只读聚合指标。"""

    llm_duration_ms: int = 0
    tool_duration_ms: int = 0
    approval_wait_ms: int = 0
    longest_tool_duration_ms: int = 0
    failed_tool_count: int = 0
    incomplete_span_count: int = 0
    critical_path_ms: int = 0

    def to_dict(self) -> dict[str, object]:
        """序列化为稳定字典。"""
        return {
            "llm_duration_ms": self.llm_duration_ms,
            "tool_duration_ms": self.tool_duration_ms,
            "approval_wait_ms": self.approval_wait_ms,
            "longest_tool_duration_ms": self.longest_tool_duration_ms,
            "failed_tool_count": self.failed_tool_count,
            "incomplete_span_count": self.incomplete_span_count,
            "critical_path_ms": self.critical_path_ms,
        }


@dataclass(frozen=True)
class RunTraceSource:
    """追踪来源的不可变元数据；``record_hash`` 指向原始权威事实。"""

    run_id: str
    session_id: str
    schema_version: str
    is_partial: bool
    record_hash: str

    def to_dict(self) -> dict[str, object]:
        """序列化为稳定字典（不含 assembled_at 等运行期时间戳）。"""
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "schema_version": self.schema_version,
            "is_partial": self.is_partial,
            "record_hash": self.record_hash,
        }


@dataclass(frozen=True)
class RunTrace:
    """从权威事实重建出来的完整运行追踪；本身为只读消费者。"""

    schema_version: str
    run: AgentRun
    source: RunTraceSource
    spans: tuple[TraceSpan, ...]
    messages: tuple[RunMessage, ...]
    context_versions: tuple[ContextVersion, ...]
    metrics: TraceMetrics
    issues: tuple[TraceIssue, ...]

    @property
    def is_partial(self) -> bool:
        """是否为语义不完整的追踪。"""
        return self.source.is_partial

    def to_dict(self, include_content: bool = False) -> dict[str, object]:
        """序列化为 JSON 兼容字典；``include_content`` 控制是否导出完整内容。"""
        return {
            "schema_version": self.schema_version,
            "source": self.source.to_dict(),
            "run": self.run.to_dict(),
            "spans": [span.to_dict() for span in self.spans],
            "messages": [_message_view(message, include_content) for message in self.messages],
            "context_versions": [
                _context_version_view(version, include_content) for version in self.context_versions
            ],
            "metrics": self.metrics.to_dict(),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _message_view(message: RunMessage, include_content: bool) -> dict[str, object]:
    """按内容模式渲染消息；默认仅导出脱敏预览与引用，不导出正文。"""
    if include_content:
        return message.to_dict()
    preview: str = message.content[:80]
    return {
        "id": message.message_id,
        "sequence": message.sequence,
        "kind": message.kind.value,
        "role": message.role.value,
        "tool_call_id": message.tool_call_id,
        "name": message.name,
        "content_length": len(message.content),
        "content_preview": preview,
    }


def _context_version_view(version: ContextVersion, include_content: bool) -> dict[str, object]:
    """按内容模式渲染上下文版本；默认仅导出引用哈希，不导出 Slot 正文。"""
    if include_content:
        return version.to_dict()
    return {
        "version": version.version,
        "created_at": version.created_at,
        "content_hash": version.content_hash,
        "tool_schema_hash": version.tool_schema_hash,
    }
