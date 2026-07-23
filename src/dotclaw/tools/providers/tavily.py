"""Tavily 搜索 Provider（Tool v1 阶段三新增）。

固定协议适配器：仅调用 ``POST /search``，读取 ``TAVILY_API_KEY`` 环境变量，请求最小
必要字段，并把 Provider JSON 映射为受限搜索结果。不请求页面正文、图片、Extract/Crawl/
Map（开发计划 §2.5）。所有新增注释使用中文。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from dotclaw.tools.base import ToolErrorCode
from dotclaw.tools.http_client import HttpClient
from dotclaw.tools.network import KNOWN_NETWORK_HOSTS

from .base import ProviderError, call, map_http_status

logger = logging.getLogger("dotclaw.tools.providers.tavily")

_TAVILY_HOST = KNOWN_NETWORK_HOSTS["tavily"][0]
_TAVILY_SEARCH_URL = f"https://{_TAVILY_HOST}/search"


@dataclass
class SearchItem:
    """单条受限搜索结果（不含页面正文/图片等未请求字段）。"""

    title: str
    url: str
    snippet: str
    score: float | None = None


@dataclass
class SearchResult:
    """一次搜索的受限结果集。"""

    query: str
    results: list[SearchItem]


class TavilyProvider:
    """固定 Tavily 搜索协议的适配器。"""

    def __init__(self, client: HttpClient) -> None:
        self._client = client

    async def search(self, query: str, max_results: int) -> SearchResult:
        """执行一次受限搜索并返回结构化结果。

        Args:
            query: 已校验的搜索词（1–256 字符）。
            max_results: 已校验的返回条数上限（1–5）。
        """
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            raise ProviderError(
                ToolErrorCode.CONFIGURATION_ERROR,
                "缺少 TAVILY_API_KEY 环境变量，无法使用搜索服务",
            )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        # 仅请求搜索最小必要字段；不携带 Extract/Crawl/Map 等网页抓取参数。
        body = {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_images": False,
            "include_image_descriptions": False,
            "include_answer": False,
            "include_raw_content": False,
        }
        resp = await call(
            self._client,
            service="tavily",
            method="POST",
            url=_TAVILY_SEARCH_URL,
            label="搜索服务",
            headers=headers,
            json=body,
        )
        if resp.status_code != 200:
            raise map_http_status("搜索服务", resp.status_code)
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            raise ProviderError(ToolErrorCode.NETWORK_ERROR, "搜索服务响应解析失败")

        raw_results = data.get("results") or []
        items = [
            SearchItem(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("content", "")),
                score=item.get("score"),
            )
            for item in raw_results[:max_results]
        ]
        return SearchResult(query=query, results=items)
