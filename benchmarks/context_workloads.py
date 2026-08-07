"""PR6 固定上下文工作负载：只提供可复跑夹具，不修改生产 Context 链路。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from dotclaw.runtime.domain.context import ContextOwner


class ContextScenario(StrEnum):
    """PR6 的有限场景标识。"""

    CONSISTENCY = "fixed_input_consistency"
    COLD_RECOVERY = "cold_recovery_v1_to_v2"
    REPLAY_EFFICIENCY = "replay_efficiency"
    COMPRESSION_SUCCESS = "compression_success"
    COMPRESSION_FAILURE = "compression_failure"
    COMPRESSION_CANCELLED = "compression_cancelled"
    COMPRESSION_ABANDONED = "compression_abandoned"
    OWNER_ISOLATION = "owner_isolation"


@dataclass(frozen=True)
class ContextFixture:
    """一个 Owner（上下文所有者）可识别的固定 Slot 夹具。"""

    slot_id: str
    owner: ContextOwner
    content: str
    injection_order: int


@dataclass(frozen=True)
class CompressionCorpus:
    """固定 Session 历史语料（不评价摘要语义）。"""

    conversations: tuple[str, ...]
    budget_window: int


def fixed_context_fixtures() -> tuple[ContextFixture, ...]:
    """返回覆盖四层 Owner 的稳定、无敏感信息夹具。"""
    return (
        ContextFixture("global_directory", ContextOwner.GLOBAL, "GLOBAL:directory", 10),
        ContextFixture("agent_identity", ContextOwner.AGENT, "AGENT:alpha", 20),
        ContextFixture("session_profile", ContextOwner.SESSION, "SESSION:one", 30),
        ContextFixture("run_retrieval", ContextOwner.RUN, "RUN:one", 40),
    )


def compression_corpus() -> CompressionCorpus:
    """返回强制触发预算分支的确定性小语料。"""
    return CompressionCorpus(
        conversations=("旧问题一 旧回答一", "旧问题二 旧回答二", "最近问题 最近回答"),
        budget_window=8,
    )
