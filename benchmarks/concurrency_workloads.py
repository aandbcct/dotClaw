"""PR3 并发 Benchmark 固定工作负载、受控延迟替身与事实读取。

本模块提供并发实验所需的基础设施，不修改 Runtime 生产代码：

- ``WorkloadConfig``：单次实验的固定条件（Session/请求数、延迟、调度模式）；
- ``ControlledSubmissionGate``：为每个 Session 分配单调 ``accepted_seq``；
- ``FixedDelayLLM`` / ``FixedDelayTool``：回显唯一标识的受控延迟替身；
- ``IdentifierCodec``：编码/解码每请求的唯一标识（session/run/request 前缀）；
- 事实读取辅助：从持久化目录读取 Run/消息/事件/ContextVersion/工具记录。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from dotclaw.runtime.adapters.run_repository import RunRepositoryAdapter
from dotclaw.runtime.application.dto import (
    ContextBundle,
    ContextMetadata,
    RunRequest,
    RunResult,
    ToolInvocation,
    ToolResult,
    ToolResultStatus,
)
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.ports import ContextPort, LLMPort, RunPolicyPort, ToolPort
from dotclaw.runtime.domain.events import RunEvent
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot,
    MessageRole,
    RunMessage,
    RunMessageKind,
)

from .eval_baseline_models import ScheduleMode

# --------------------------------------------------------------------------- #
# 标识编码：唯一 session/run/request 前缀
# --------------------------------------------------------------------------- #

# 标识前缀常量：用于编码请求唯一性和验证跨 Session 串扰
SESSION_PREFIX: str = "s"
"""Session 标识前缀。"""

RUN_PREFIX: str = "r"
"""Run 标识前缀。"""

REQUEST_PREFIX: str = "req"
"""请求标识前缀。"""


class IdentifierCodec:
    """为每个请求生成唯一标识，并可从事实中解码验证。

    标识格式：``{SESSION_PREFIX}{session_index}_{RUN_PREFIX}{run_index}_{REQUEST_PREFIX}``
    示例：``s0_r2_req`` 表示 Session 0 的第 2 个 Run。

    所有 Fake LLM/Tool 回复均回显此标识，供跨 Session 串扰检测。
    """

    @staticmethod
    def encode(session_index: int, run_index: int) -> str:
        """编码一个请求的唯一标识字符串。"""
        return f"{SESSION_PREFIX}{session_index}_{RUN_PREFIX}{run_index}_{REQUEST_PREFIX}"

    @staticmethod
    def session_prefix(session_index: int) -> str:
        """仅编码 Session 级标识前缀。"""
        return f"{SESSION_PREFIX}{session_index}"

    @staticmethod
    def extract_session_indices(content: str) -> set[int]:
        """从内容中提取所有引用的 Session 索引（用于跨 Session 串扰检测）。"""
        indices: set[int] = set()
        for word in content.replace("_", " ").split():
            if word.startswith(SESSION_PREFIX) and word[len(SESSION_PREFIX):].isdigit():
                indices.add(int(word[len(SESSION_PREFIX):]))
        return indices


# --------------------------------------------------------------------------- #
# 工作负载配置
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WorkloadConfig:
    """一次并发实验的固定条件。

    所有字段均写入 workload-config.json 与每条 BenchmarkSample，
    schedule_mode 为 None 表示该样本来自 PR1 Eval（非并发实验）。
    """

    session_count: int
    """该轮实验的 Session 数。"""

    requests_per_session: int
    """每个 Session 的请求数。"""

    fake_delay_ms: int
    """固定延迟替身的延迟毫秒数（短延迟默认 20ms，长延迟默认 200ms）。"""

    schedule_mode: ScheduleMode
    """调度模式（Session 锁或全局锁）。"""

    warmup: int
    """预热轮数。"""

    repeat: int
    """正式采样轮数。"""

    long_delay_ms: int | None = None
    """长延迟毫秒数（仅长短混合场景使用）。"""

    long_request_session_index: int = 0
    """长请求所在 Session 的索引（仅长短混合场景使用）。"""

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "session_count": self.session_count,
            "requests_per_session": self.requests_per_session,
            "fake_delay_ms": self.fake_delay_ms,
            "schedule_mode": self.schedule_mode.value,
            "warmup": self.warmup,
            "repeat": self.repeat,
            "long_delay_ms": self.long_delay_ms,
            "long_request_session_index": self.long_request_session_index,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> WorkloadConfig:
        """从 JSON 字典严格反序列化。"""
        return cls(
            session_count=int(data["session_count"]),
            requests_per_session=int(data["requests_per_session"]),
            fake_delay_ms=int(data["fake_delay_ms"]),
            schedule_mode=ScheduleMode(str(data["schedule_mode"])),
            warmup=int(data["warmup"]),
            repeat=int(data["repeat"]),
            long_delay_ms=int(data["long_delay_ms"]) if data.get("long_delay_ms") is not None else None,
            long_request_session_index=int(data.get("long_request_session_index", 0)),
        )


# --------------------------------------------------------------------------- #
# 受控提交闸门
# --------------------------------------------------------------------------- #


class ControlledSubmissionGate:
    """为每个 Session 分配单调递增的 ``accepted_seq``。

    使用同步计数器按 API 接受顺序分配序号，避免不可控的协程调度先后干扰。
    每个 Session 独立计序，互不影响。
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        """Session ID -> 当前序号。"""

    def accept(self, session_id: str) -> int:
        """为指定 Session 分配下一个单调递增的接受序号（1-based）。

        返回：分配的 ``accepted_seq``。
        """
        current: int = self._counters.get(session_id, 0)
        new_seq: int = current + 1
        self._counters[session_id] = new_seq
        return new_seq

    def reset(self) -> None:
        """重置所有 Session 的计数器（每轮开始时调用）。"""
        self._counters.clear()


# --------------------------------------------------------------------------- #
# 受控延迟替身
# --------------------------------------------------------------------------- #


class FixedDelayLLM(LLMPort):
    """固定延迟 LLM 替身：回显唯一标识 + 固定延迟后返回最终回答。

    每个请求的回复内容包含其 ``session_index`` 和 ``run_index`` 标识，
    供跨 Session 串扰检测。
    """

    def __init__(self, delay_ms: int = 20) -> None:
        """初始化固定延迟 LLM 替身。

        参数：
            delay_ms: 每次 ``complete()`` 的固定延迟毫秒数。
        """
        self._delay_ms: int = delay_ms
        self._call_count: int = 0
        self._message_seq: int = 0

    async def complete(self, context: ContextBundle, execution: RunExecutionView, output_port=None) -> RunMessage:
        """模拟 LLM 调用：固定延迟后返回包含会话标识的回答。"""
        # 从用户消息中提取标识（由请求构造时注入）
        user_content: str = ""
        for msg in context.messages:
            if msg.content:
                user_content = msg.content
                break

        # 延迟模拟
        self._call_count += 1
        await asyncio.sleep(self._delay_ms / 1000.0)

        self._message_seq += 1
        return RunMessage(
            message_id=f"llm-{self._call_count}",
            sequence=self._message_seq,
            kind=RunMessageKind.FINAL_RESPONSE,
            role=MessageRole.ASSISTANT,
            content=f"回答来自: {user_content}",
        )

    async def cancel(self, run_id: str) -> None:
        """取消当前 LLM 调用（不阻塞）。"""
        pass


class LongDelayLLM(FixedDelayLLM):
    """长延迟 LLM 替身：用于长短混合和取消不阻塞场景。"""

    def __init__(self, delay_ms: int = 200, cancel_barrier: asyncio.Event | None = None) -> None:
        """初始化长延迟 LLM 替身。

        参数：
            delay_ms: 延迟毫秒数（默认 200ms）。
            cancel_barrier: 取消屏障事件；设置后 LLM 会在延迟期间等待此事件，
                用于取消场景中让 Benchmark 有机会在 LLM 完成前发送取消信号。
        """
        super().__init__(delay_ms)
        self._cancel_barrier: asyncio.Event | None = cancel_barrier
        self.was_cancelled: bool = False

    async def complete(self, context: ContextBundle, execution: RunExecutionView, output_port=None) -> RunMessage:
        """长延迟 LLM 调用：等待屏障后再返回。"""
        user_content: str = ""
        for msg in context.messages:
            if msg.content:
                user_content = msg.content
                break

        self._call_count += 1
        # 分段延迟：先等待一半时间，检查取消屏障，再等待剩余
        half_delay: float = self._delay_ms / 2000.0
        await asyncio.sleep(half_delay)
        if self._cancel_barrier is not None:
            self._cancel_barrier.set()  # 通知 Benchmark：此时可发送取消
        await asyncio.sleep(half_delay)

        self._message_seq += 1
        return RunMessage(
            message_id=f"llm-long-{self._call_count}",
            sequence=self._message_seq,
            kind=RunMessageKind.FINAL_RESPONSE,
            role=MessageRole.ASSISTANT,
            content=f"长回答来自: {user_content}",
        )

    async def cancel(self, run_id: str) -> None:
        """标记已取消（不阻塞）。"""
        self.was_cancelled = True


class FixedDelayTool(ToolPort):
    """固定延迟工具替身：回显唯一标识 + 固定延迟后返回完成。

    工具输出内容包含其所属的 ``session_index`` 和 ``run_index`` 标识。
    """

    def __init__(self, delay_ms: int = 20) -> None:
        """初始化固定延迟工具替身。

        参数：
            delay_ms: 每次 ``execute()`` 的固定延迟毫秒数。
        """
        self._delay_ms: int = delay_ms
        self._call_count: int = 0

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """模拟工具执行：固定延迟后返回带标识的输出。"""
        self._call_count += 1
        await asyncio.sleep(self._delay_ms / 1000.0)
        return ToolResult(
            call_id=invocation.call.call_id,
            status=ToolResultStatus.COMPLETED,
            output=f"工具执行结果: call-{self._call_count}",
        )

    async def cancel(self, run_id: str) -> None:
        """取消当前工具执行（不阻塞）。"""
        pass


class FixedContext(ContextPort):
    """固定上下文替身：仅返回一条包含请求标识的 system 消息。"""

    def __init__(self, system_message: str = "你是一个受控 Benchmark 替身") -> None:
        self._system_message: str = system_message

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """构建最小上下文：一条 system 消息。"""
        msg = RunMessage(
            message_id="ctx-1",
            sequence=1,
            kind=RunMessageKind.LLM_REQUEST,
            role=MessageRole.SYSTEM,
            content=self._system_message,
        )
        return ContextBundle((msg,), (), ContextMetadata(estimated_tokens=1))

    async def release_scope(self, owner, owner_key) -> None:
        pass

    async def release_all(self) -> None:
        pass

    def request_refresh(self, slot_id, owner, owner_key) -> None:
        pass

    def publish_signal(self, signal) -> None:
        pass


class FixedPolicy(RunPolicyPort):
    """固定策略替身：返回最小合法 AgentPolicySnapshot。"""

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        return AgentPolicySnapshot(
            agent_id=request.agent_id,
            identity_version="bench-v1",
            model_name="bench-model",
            max_iterations=3,
            policy_data={
                "context_window": 2048,
                "tokenizer_encoding": "cl100k_base",
            },
        )


# --------------------------------------------------------------------------- #
# 事实读取：从持久化目录读取运行证据
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RunFacts:
    """一个 Run 持久化后的全部可读事实。"""

    run_id: str
    """Run 标识。"""

    session_id: str
    """所属 Session 标识。"""

    state_outcome: str
    """Run 终态（如 "completed"、"cancelled"）。"""

    messages: tuple[RunMessage, ...]
    """Run 期间产生的全部消息。"""

    events: tuple[RunEvent, ...]
    """Run 期间产生的全部事件。"""

    final_message_content: str
    """最终回答正文。"""

    identifier: str
    """从请求中提取的唯一标识（session/run 前缀）。"""


async def read_run_facts(
    repository: RunRepositoryAdapter,
    session_id: str,
    run_id: str,
) -> RunFacts | None:
    """从持久化目录读取一个 Run 的全部可读事实。

    返回 None 表示该 Run 尚未持久化（可能是 warmup 或已删除）。
    """
    run = await repository.load_run(session_id, run_id)
    if run is None:
        return None

    messages: tuple[RunMessage, ...] = await repository.load_messages(session_id, run_id)
    events: tuple[RunEvent, ...] = await repository.load_events(session_id, run_id)

    # 提取最终回答
    final_content: str = ""
    for msg in reversed(messages):
        if msg.content and msg.role == MessageRole.ASSISTANT:
            final_content = msg.content
            break

    # 从消息内容中提取唯一标识
    identifier: str = ""
    for msg in messages:
        if msg.role == MessageRole.USER and msg.content:
            identifier = msg.content
            break

    return RunFacts(
        run_id=run_id,
        session_id=session_id,
        state_outcome=_render_outcome(run.state),
        messages=messages,
        events=events,
        final_message_content=final_content,
        identifier=identifier,
    )


def _render_outcome(state) -> str:
    """从 AgentRunState 提取终态字符串。"""
    from dotclaw.runtime.domain.state import Ended
    if isinstance(state.mode, Ended):
        return state.mode.outcome.value
    return str(type(state.mode).__name__)


async def read_session_conversation(
    repository: RunRepositoryAdapter,
    session_id: str,
) -> list[str]:
    """读取一个 Session 的 Conversation 投影（按序消息内容列表）。

    用于验证 Conversation 持久化顺序与 accepted_seq 一致。
    """
    conversation = await repository.load_conversation(session_id)
    result: list[str] = []
    for msg in conversation:
        if msg.content:
            result.append(msg.content)
    return result


# --------------------------------------------------------------------------- #
# 请求组装
# --------------------------------------------------------------------------- #


def make_benchmark_request(
    agent_id: str,
    session_index: int,
    run_index: int,
) -> tuple[str, str]:
    """构造 Benchmark 用的用户消息内容与唯一标识。

    返回：``(identifier, user_message)``。
    identifer 格式：``s{session_index}_r{run_index}_req``
    user_message 包含相同标识供 LLM/Tool 回显。
    """
    identifier: str = IdentifierCodec.encode(session_index, run_index)
    user_message: str = f"{identifier} 请回答"
    return identifier, user_message
