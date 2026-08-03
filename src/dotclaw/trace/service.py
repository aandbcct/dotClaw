"""RunTrace 读取服务。

``TraceService`` 是 Runtime 权威事实的只读外部消费者：只通过 ``RunRepository``
加载 Run / Event / Message / ContextVersion，交给纯函数 ``assemble_trace`` 重建。
它不写任何文件、不修改 Runtime、不在读取失败时返回伪 Trace（找不到 Run 抛明确
``LookupError``）。
"""

from __future__ import annotations

from ..runtime.application.ports import RunRepository
from ..runtime.domain.context import ContextVersion
from ..runtime.domain.events import RunEvent
from ..runtime.domain.facts import AgentRun, RunMessage
from .assembler import assemble_trace
from .models import RunTrace


class TraceService:
    """按 run_id 重建 RunTrace 的读取服务。"""

    def __init__(self, repository: RunRepository) -> None:
        """绑定运行时仓储。"""
        self._repository = repository

    async def get_trace(self, run_id: str) -> RunTrace:
        """重建指定运行的完整追踪；找不到 Run 抛 ``LookupError``。"""
        run: AgentRun | None = await self._repository.find_run(run_id)
        if run is None:
            raise LookupError(f"找不到运行 {run_id}")
        events: tuple[RunEvent, ...] = await self._repository.load_events(run.session_id, run_id)
        messages: tuple[RunMessage, ...] = await self._repository.load_messages(run.session_id, run_id)
        context_versions: tuple[ContextVersion, ...] = await self._repository.load_context_versions(
            run.session_id, run_id
        )
        return assemble_trace(run, events, messages, context_versions)
