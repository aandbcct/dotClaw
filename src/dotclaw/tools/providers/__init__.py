"""固定网络服务 Provider 包（Tool v1 阶段三新增）。

Provider 是固定外部协议的适配器：只负责构造最小必要请求、调用受控 HttpClient、
把 Provider JSON 映射为受限的业务结果或统一的 ProviderError。Provider 不出现在 Agent
Tool Schema、Tool Registry 或用户提示词中；端点、方法与认证方式属于 Provider 代码，
不允许由 Agent 参数或 YAML 覆盖（开发计划 §2.1）。

所有新增注释使用中文。
"""

from __future__ import annotations

from .base import ProviderError, map_http_status
from .open_meteo import OpenMeteoProvider
from .tavily import TavilyProvider

__all__ = ["ProviderError", "TavilyProvider", "OpenMeteoProvider", "map_http_status"]
