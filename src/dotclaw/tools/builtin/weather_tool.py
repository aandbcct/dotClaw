"""内置天气工具（builtin 子包 — Tool v1 阶段三）。

工具名：builtin.weather.get_forecast。固定调用 Open-Meteo 固定 Provider；经 Tool v1
安全链路执行。网络 Tool 不存在 Agent 可控 URL/host/endpoint 参数（开发计划 §2.2）。
所有新增注释使用中文。
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from pydantic import AfterValidator

from dotclaw.tools.base import ToolContext, ToolErrorCode, ToolResult
from dotclaw.tools.decorator import ToolPolicy, tool
from dotclaw.tools.network import KNOWN_NETWORK_HOSTS
from dotclaw.tools.providers import OpenMeteoProvider, ProviderError

logger = logging.getLogger("dotclaw.tools.builtin.weather")


def _validate_location(value: str) -> str:
    """清理地点名称首尾空白并保留原长度限制。"""
    value = value.strip()
    if not (2 <= len(value) <= 120):
        raise ValueError("location 需为去除首尾空白后 2–120 字符")
    return value


def _validate_country_code(value: str | None) -> str | None:
    """清理并大写国家码后保留原格式校验。"""
    if value is None:
        return None
    value = value.strip().upper()
    if len(value) != 2 or not value.isalpha():
        raise ValueError("country_code 需为两位 ISO 3166-1 alpha-2 代码")
    return value


def _validate_days(value: int) -> int:
    """保留预报天数边界校验。"""
    if not (1 <= value <= 7):
        raise ValueError("days 需为 1–7")
    return value


@tool(
    name="builtin.weather.get_forecast",
    description=(
        "查询指定地点的天气预报（数据来自 Open-Meteo）。输入地点名称，可选两位国家码"
        "缩小范围与预报天数（1–7）。返回当前天气与每日预报；若地点存在多个候选，"
        "会列出候选供你向用户确认，不会自行猜测。地点名来自外部，仅作信息参考。"
    ),
    policy=ToolPolicy.NETWORK,
    network_service="open_meteo",
    network_hosts=KNOWN_NETWORK_HOSTS["open_meteo"],
)
async def get_forecast(
    location: Annotated[str, AfterValidator(_validate_location)],
    country_code: Annotated[str | None, AfterValidator(_validate_country_code)] = None,
    days: Annotated[int, AfterValidator(_validate_days)] = 3,
    *, context: ToolContext,
) -> ToolResult:
    """执行受限天气查询，返回稳定的业务结构（预报/候选/无候选）。"""
    client = context.http_client
    if client is None:
        return ToolResult.from_error(
            code=ToolErrorCode.CONFIGURATION_ERROR,
            message="网络客户端未初始化，无法使用天气服务",
        )

    provider = OpenMeteoProvider(client)
    try:
        result = await provider.get_forecast(location, country_code, days)
    except ProviderError as exc:
        return ToolResult.from_error(code=exc.code, message=str(exc))

    return ToolResult(output=json.dumps(result, ensure_ascii=False))
