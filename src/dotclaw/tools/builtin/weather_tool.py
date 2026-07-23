"""内置天气工具（builtin 子包 — Tool v1 阶段三）。

工具名：builtin.weather.get_forecast。固定调用 Open-Meteo 固定 Provider；经 Tool v1
安全链路执行。网络 Tool 不存在 Agent 可控 URL/host/endpoint 参数（开发计划 §2.2）。
所有新增注释使用中文。
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ConfigDict, field_validator

from dotclaw.tools.base import ToolContext, ToolErrorCode, ToolResult
from dotclaw.tools.decorator import ToolPolicy, tool
from dotclaw.tools.network import KNOWN_NETWORK_HOSTS
from dotclaw.tools.providers import OpenMeteoProvider, ProviderError

logger = logging.getLogger("dotclaw.tools.builtin.weather")


class WeatherArgs(BaseModel):
    """天气参数（显式 Pydantic 模型，严格校验；extra=forbid）。"""

    model_config = ConfigDict(extra="forbid")

    location: str
    country_code: str | None = None
    days: int = 3

    @field_validator("location")
    @classmethod
    def _validate_location(cls, v: str) -> str:
        v = v.strip()
        if not (2 <= len(v) <= 120):
            raise ValueError("location 需为去除首尾空白后 2–120 字符")
        return v

    @field_validator("country_code")
    @classmethod
    def _validate_country_code(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if len(v) != 2 or not v.isalpha():
            raise ValueError("country_code 需为两位 ISO 3166-1 alpha-2 代码")
        return v

    @field_validator("days")
    @classmethod
    def _validate_days(cls, v: int) -> int:
        if not (1 <= v <= 7):
            raise ValueError("days 需为 1–7")
        return v


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
    args_model=WeatherArgs,
)
async def get_forecast(args: WeatherArgs, context: ToolContext) -> ToolResult:
    """执行受限天气查询，返回稳定的业务结构（预报/候选/无候选）。"""
    client = context.http_client
    if client is None:
        return ToolResult.from_error(
            code=ToolErrorCode.CONFIGURATION_ERROR,
            message="网络客户端未初始化，无法使用天气服务",
        )

    provider = OpenMeteoProvider(client)
    try:
        result = await provider.get_forecast(args.location, args.country_code, args.days)
    except ProviderError as exc:
        return ToolResult.from_error(code=exc.code, message=str(exc))

    return ToolResult(output=json.dumps(result, ensure_ascii=False))
