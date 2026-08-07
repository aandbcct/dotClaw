"""PR5 安全链阶段观察器：只通过公开依赖注入读取事实。"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

from dotclaw.tools.approval import ApprovalManager
from dotclaw.tools.base import ToolExecutionContext, ToolResult
from dotclaw.tools.capability import CapabilityBroker, CapabilityRequest
from dotclaw.tools.handler import ToolHandler
from dotclaw.tools.policy import PolicyEngine, PolicyOutcome


@dataclass
class ChainObservation:
    """一次调用的阶段计数、资源摘要与脱敏观察。"""

    broker_entered: int = 0
    policy_entered: int = 0
    approval_entered: int = 0
    handler_entered: int = 0
    requests: list[CapabilityRequest] = field(default_factory=list)
    outcome: PolicyOutcome | None = None
    handler_paths: list[str] = field(default_factory=list)
    approval_summaries: list[str] = field(default_factory=list)
    journal_summaries: list[str] = field(default_factory=list)
    handler_entry_at: float | None = None

    def sensitive_leak_count(self, marker: str) -> int:
        """统计观察到的摘要中测试敏感标记出现次数。"""
        return sum(summary.count(marker) for summary in self.approval_summaries + self.journal_summaries)


class CountingBroker(CapabilityBroker):
    """计数型 Broker（资源解析器）：保留生产解析语义。"""

    def __init__(self, observation: ChainObservation) -> None:
        super().__init__()
        self._observation = observation

    def resolve(self, definition: Any, validated_args: Any, workspace_root: str) -> list[CapabilityRequest]:
        self._observation.broker_entered += 1
        requests = super().resolve(definition, validated_args, workspace_root)
        self._observation.requests = requests
        return requests


class CountingPolicyEngine(PolicyEngine):
    """计数型 Policy Engine（策略引擎）：保留生产判断语义。"""

    def __init__(self, observation: ChainObservation, scope) -> None:
        super().__init__(scope)
        self._observation = observation

    def evaluate(self, requests: list[CapabilityRequest], scope=None) -> PolicyOutcome:
        self._observation.policy_entered += 1
        outcome = super().evaluate(requests, scope)
        self._observation.outcome = outcome
        return outcome


class CountingApprovalManager(ApprovalManager):
    """计数型审批端口：记录传入的已脱敏摘要。"""

    def __init__(self, observation: ChainObservation) -> None:
        super().__init__()
        self._observation = observation

    async def request(self, summary: str, channel: Any | None = None) -> bool:
        self._observation.approval_entered += 1
        self._observation.approval_summaries.append(summary)
        return await super().request(summary, channel)


class CountingHandler(ToolHandler):
    """记录型 Handler（处理器）：委托原 Handler 且不产生外部副作用。"""

    def __init__(self, inner: ToolHandler, observation: ChainObservation) -> None:
        self._inner = inner
        self._observation = observation

    def definition(self):
        return self._inner.definition()

    @property
    def args_model(self):
        return self._inner.args_model

    @property
    def input_schema(self):
        return self._inner.input_schema

    async def execute(self, arguments: Any, context: ToolExecutionContext | None = None) -> ToolResult:
        self._observation.handler_entry_at = time.perf_counter()
        self._observation.handler_entered += 1
        path = getattr(arguments, "path", None)
        if isinstance(path, str):
            self._observation.handler_paths.append(path)
        return await self._inner.execute(arguments, context)


class RecordingJournal:
    """最小 Journal（审计记录器）替身：只记录安全摘要，不保存原始参数。"""

    def __init__(self, observation: ChainObservation) -> None:
        self._observation = observation

    def tool_start(self, name: str) -> None:
        return None

    def tool_end(self, name: str, **kwargs: Any) -> None:
        return None

    def tool_policy_resolved(self, name: str, decision: str, rule: str, summary: str) -> None:
        self._observation.journal_summaries.append(summary)

    def tool_approval_outcome(self, name: str, outcome: str, summary: str) -> None:
        self._observation.journal_summaries.append(summary)
