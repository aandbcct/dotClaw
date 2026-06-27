"""千问（Qwen）LLM 客户端实现

基于 OpenAICompatibleClient，仅覆写 provider 特定的钩子方法。
"""

from __future__ import annotations

from openai import AsyncOpenAI

from ..openai_compat import OpenAICompatibleClient
from . import register


@register("qwen")
class QwenClient(OpenAICompatibleClient):
    """
    千问 API 客户端。

    千问兼容 OpenAI SDK，只需改 base_url 和 model。
    """

    def __init__(self, api_key: str, base_url: str, model: str):
        super().__init__()
        self._api_key = api_key
        self._base_url = base_url
        self._model = model

    def _get_api_key(self) -> str:
        return self._api_key

    def _get_base_url(self) -> str:
        return self._base_url

    def _get_model_id(self) -> str:
        return self._model

    def _get_client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=self._get_api_key(),
            base_url=self._get_base_url(),
        )
