"""将 Runtime 语义化增量事件按分区展示适配为 Channel 输出。"""

from __future__ import annotations

from ..runtime.application.dto import LLMOutputEvent, LLMOutputKind
from ..runtime.application.ports import LLMOutputPort
from .base import Channel


class ChannelLLMOutputAdapter:
    """将 Runtime 的模型增量事件分区展示到当前 Channel。

    按 ``run_id`` 记忆上次展示的语义类别；切换到 reasoning/response 时打印一次
    「思考」/「回答」分区标题，连续同类增量不重复标题。模型文本与标题均经
    ``Channel.stream`` 的纯文本路径输出，不被解释为 Rich markup。

    入口层不把 reasoning 专有方法加入通用 ``Channel``（总体设计 §6）；只依赖
    Runtime application 的 DTO/Port，符合 §4 依赖方向。
    """

    def __init__(self, channel: Channel) -> None:
        """绑定入口层拥有的 Channel，不向 Runtime 泄漏具体通道类型。"""
        self._channel: Channel = channel
        # 每个运行单独记忆上次展示语义，避免不同 Run 的标题状态互相污染。
        self._last_kind: dict[str, LLMOutputKind | None] = {}

    async def emit(self, event: LLMOutputEvent) -> None:
        """按语义分区转发增量；空文本不展示、不打标题。

        标题仅在语义类别相对本 Run 上次展示发生变化（含首次展示）时打印一次，
        保证 reasoning 与 response 各自成段，且连续同类增量不重复标题。
        """
        if not event.content:
            # 空增量无展示价值，既不输出文本也不切换标题状态。
            return

        last_kind: LLMOutputKind | None = self._last_kind.get(event.run_id)
        if last_kind is not event.kind:
            title: str = "思考" if event.kind is LLMOutputKind.REASONING_DELTA else "回答"
            await self._channel.stream(f"\n{title}：\n")
            self._last_kind[event.run_id] = event.kind

        await self._channel.stream(event.content)
