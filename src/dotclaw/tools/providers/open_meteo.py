"""Open-Meteo 天气 Provider（Tool v1 阶段三新增）。

固定协议适配器：先地理编码解析经纬度，再以 ``timezone=auto`` 请求当前天气与每日预报；
预报请求固定当前/每日字段，不机械透传 Open-Meteo 的全部参数（开发计划 §2.5）。
地点候选处理：唯一候选直接返回预报；零候选返回稳定业务结构；多候选返回至多 5 个
候选供 Agent 向用户追问，不静默猜测。所有新增注释使用中文。
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote

from dotclaw.tools.base import ToolErrorCode
from dotclaw.tools.http_client import HttpClient
from dotclaw.tools.network import KNOWN_NETWORK_HOSTS

from .base import ProviderError, call, map_http_status

logger = logging.getLogger("dotclaw.tools.providers.open_meteo")

_GEO_HOST = KNOWN_NETWORK_HOSTS["open_meteo"][0]  # geocoding-api.open-meteo.com
_FC_HOST = KNOWN_NETWORK_HOSTS["open_meteo"][1]   # api.open-meteo.com
_GEO_URL = f"https://{_GEO_HOST}/v1/search"
_FC_URL = f"https://{_FC_HOST}/v1/forecast"

# 固定请求的当前天气字段与每日字段（不暴露全部参数/模型/历史数据/单位选项）。
_CURRENT_FIELDS = "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m"
_DAILY_FIELDS = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"

# 多候选时向 Agent 返回的候选数量上限（开发计划 §2.5）。
_MAX_CANDIDATES = 5


class OpenMeteoProvider:
    """固定 Open-Meteo 天气协议的适配器。"""

    def __init__(self, client: HttpClient) -> None:
        self._client = client

    async def geocode(self, location: str, country_code: str | None = None) -> list[dict]:
        """地理编码：返回候选地点列表（可能为空或多条）。"""
        params = f"name={quote(location)}&count=10&language=en&format=json"
        if country_code:
            # country_code 缩小候选范围（开发计划阶段三验收）。
            params += f"&countryCode={quote(country_code)}"
        url = f"{_GEO_URL}?{params}"
        # 仅对临时网络错误重试一次（开发计划 §2.4）。
        resp = await call(
            self._client,
            service="open_meteo",
            method="GET",
            url=url,
            label="天气服务",
            retry_once=True,
        )
        if resp.status_code != 200:
            raise map_http_status("天气服务", resp.status_code)
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            raise ProviderError(ToolErrorCode.NETWORK_ERROR, "天气服务地理编码响应解析失败")
        return data.get("results") or []

    async def forecast(self, latitude: float, longitude: float, days: int) -> dict:
        """请求固定字段的当前天气与每日预报。"""
        params = (
            f"latitude={latitude}&longitude={longitude}"
            f"&current={_CURRENT_FIELDS}"
            f"&daily={_DAILY_FIELDS}"
            f"&timezone=auto&forecast_days={days}"
        )
        url = f"{_FC_URL}?{params}"
        resp = await call(
            self._client,
            service="open_meteo",
            method="GET",
            url=url,
            label="天气服务",
            retry_once=True,
        )
        if resp.status_code != 200:
            raise map_http_status("天气服务", resp.status_code)
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            raise ProviderError(ToolErrorCode.NETWORK_ERROR, "天气服务预报响应解析失败")
        return data

    async def get_forecast(
        self, location: str, country_code: str | None, days: int
    ) -> dict:
        """端到端：地理编码 → 候选处理 →（唯一候选）预报。

        返回稳定的业务结构：
        - 零候选：{"type": "no_candidate", ...}
        - 多候选：{"type": "candidates", "candidates": [...]}（至多 5 个）
        - 唯一候选：{"type": "forecast", "location": ..., "current": ..., "daily": ...}
        """
        candidates = await self.geocode(location, country_code)
        if not candidates:
            return {"type": "no_candidate", "location": location}
        if len(candidates) > 1:
            trimmed = [
                {
                    "name": c.get("name", ""),
                    "country": c.get("country", ""),
                    "admin1": c.get("admin1", ""),
                    "latitude": c.get("latitude"),
                    "longitude": c.get("longitude"),
                }
                for c in candidates[:_MAX_CANDIDATES]
            ]
            return {"type": "candidates", "location": location, "candidates": trimmed}

        c = candidates[0]
        fc = await self.forecast(c["latitude"], c["longitude"], days)
        return {
            "type": "forecast",
            "location": {
                "name": c.get("name", ""),
                "country": c.get("country", ""),
                "admin1": c.get("admin1", ""),
                "latitude": c.get("latitude"),
                "longitude": c.get("longitude"),
                "timezone": c.get("timezone", ""),
            },
            "current": fc.get("current", {}),
            "daily": fc.get("daily", {}),
        }
