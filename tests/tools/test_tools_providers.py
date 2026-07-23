"""Tavily / Open-Meteo Provider 与网络 Tool 的 Mock 契约测试（Tool v1 阶段三）。

覆盖开发计划阶段三验收：成功/无密钥/401/429/5xx/超时/畸形响应；搜索请求不含网页
提取参数且输出不超过上限；天气唯一/零/多候选、country_code 缩小范围、days 边界；
所有网络调用均经 Broker/Policy，Journal 只记录脱敏网络摘要。

全程使用 Fake/Mock HttpClient，不访问真实 Tavily 或 Open-Meteo。所有新增注释使用中文。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from dotclaw.bootstrap._host_components import _build_tools
from dotclaw.config.settings import Config, NetworkServiceConfig, NetworkToolsConfig
from dotclaw.tools.base import ToolErrorCode, ToolExecutionContext
from dotclaw.tools.builtin.web_tool import SearchArgs, web_search
from dotclaw.tools.builtin.weather_tool import WeatherArgs, get_forecast
from dotclaw.tools.http_client import HttpClientError, ProviderHttpResponse
from dotclaw.tools.providers import OpenMeteoProvider, TavilyProvider


class FakeHttpClient:
    """测试用 Fake HttpClient：按队列返回 ProviderHttpResponse，并忠实记录每次调用。

    用于在不触达真实网络的前提下验证 Provider 的映射与脱敏行为（开发计划阶段三）。
    """

    def __init__(
        self,
        responses: list[ProviderHttpResponse] | None = None,
        *,
        raise_exc: Exception | None = None,
        record: list[dict] | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._raise = raise_exc
        self.calls = record if record is not None else []
        self._closed = False

    async def request(
        self,
        *,
        service: str,
        method: str,
        url: str,
        headers: dict | None = None,
        json: Any | None = None,
        retry_once: bool = False,
    ) -> ProviderHttpResponse:
        self.calls.append(
            {
                "service": service,
                "method": method,
                "url": url,
                "headers": headers,
                "json": json,
                "retry_once": retry_once,
            }
        )
        if self._raise is not None:
            raise self._raise
        if not self._responses:
            raise AssertionError("FakeHttpClient 响应队列已空")
        return self._responses.pop(0)

    async def close(self) -> None:
        self._closed = True


def _ctx(client) -> ToolExecutionContext:
    return ToolExecutionContext(timeout=30.0, http_client=client)


def _ok(text: str) -> ProviderHttpResponse:
    return ProviderHttpResponse(status_code=200, text=text, headers={})


# ───────────────────────── Tavily 搜索 ─────────────────────────


async def test_tavily_search_success_maps_and_truncates(monkeypatch) -> None:
    """成功响应映射为受限结果，且条数不超过 max_results、输出不超过总上限。"""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    body = {
        "results": [
            {
                "title": f"标题{i}",
                "url": f"https://example.com/{i}",
                "content": f"摘要内容{i}",
                "score": 0.9 - i * 0.1,
            }
            for i in range(8)
        ]
    }
    client = FakeHttpClient([_ok(json.dumps(body))])
    result = await web_search(SearchArgs(query="python", max_results=5), _ctx(client))
    assert not result.is_error
    data = json.loads(result.output)
    assert data["query"] == "python"
    # 至多 5 项（max_results 限制）。
    assert len(data["results"]) == 5


async def test_tavily_search_request_has_no_extract_crawl_map(monkeypatch) -> None:
    """搜索请求体不含 Extract/Crawl/Map 等网页抓取参数（开发计划 §2.5）。"""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    client = FakeHttpClient([_ok(json.dumps({"results": []}))])
    await web_search(SearchArgs(query="python"), _ctx(client))
    sent = client.calls[0]
    assert sent["service"] == "tavily"
    assert sent["method"] == "POST"
    assert sent["url"] == "https://api.tavily.com/search"
    payload = sent["json"]
    # 仅请求最小必要字段；不得出现网页正文抓取类参数。
    assert payload["query"] == "python"
    assert payload["max_results"] == 5
    assert payload["include_images"] is False
    # 不得请求 Extract/Crawl/Map 等网页抓取功能（include_raw_content=False 是显式关闭，允许）。
    for forbidden in ("extract", "crawl", "map"):
        assert forbidden not in payload, f"搜索请求不应包含 {forbidden}"


async def test_tavily_search_no_api_key(monkeypatch) -> None:
    """缺失 TAVILY_API_KEY 返回 CONFIGURATION_ERROR，且不读取 YAML。"""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    client = FakeHttpClient([_ok(json.dumps({"results": []}))])
    result = await web_search(SearchArgs(query="python"), _ctx(client))
    assert result.is_error
    assert result.error_code == ToolErrorCode.CONFIGURATION_ERROR.value
    # 未向网络发起请求（缺失密钥在 Provider 内提前拦截）。
    assert client.calls == []


async def test_tavily_search_auth_failure_401(monkeypatch) -> None:
    """401 鉴权失败映射为 CONFIGURATION_ERROR（脱敏，不含密钥）。"""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    client = FakeHttpClient([ProviderHttpResponse(status_code=401, text="{}", headers={})])
    result = await web_search(SearchArgs(query="python"), _ctx(client))
    assert result.is_error
    assert result.error_code == ToolErrorCode.CONFIGURATION_ERROR.value
    assert "TAVILY_API_KEY" not in result.output


async def test_tavily_search_rate_limited_429(monkeypatch) -> None:
    """429 限流映射为 NETWORK_ERROR。"""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    client = FakeHttpClient([ProviderHttpResponse(status_code=429, text="{}", headers={})])
    result = await web_search(SearchArgs(query="python"), _ctx(client))
    assert result.is_error
    assert result.error_code == ToolErrorCode.NETWORK_ERROR.value


async def test_tavily_search_server_error_5xx(monkeypatch) -> None:
    """5xx 服务错误映射为 NETWORK_ERROR。"""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    client = FakeHttpClient([ProviderHttpResponse(status_code=502, text="{}", headers={})])
    result = await web_search(SearchArgs(query="python"), _ctx(client))
    assert result.is_error
    assert result.error_code == ToolErrorCode.NETWORK_ERROR.value


async def test_tavily_search_malformed_json(monkeypatch) -> None:
    """畸形响应体解析失败映射为 NETWORK_ERROR，且不回显响应正文。"""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    client = FakeHttpClient([ProviderHttpResponse(status_code=200, text="not-json", headers={})])
    result = await web_search(SearchArgs(query="python"), _ctx(client))
    assert result.is_error
    assert result.error_code == ToolErrorCode.NETWORK_ERROR.value


async def test_tavily_search_network_error(monkeypatch) -> None:
    """HttpClient 抛出的脱敏网络异常映射为 NETWORK_ERROR。"""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    client = FakeHttpClient([], raise_exc=HttpClientError("网络请求失败（api.tavily.com）：ConnectError"))
    result = await web_search(SearchArgs(query="python"), _ctx(client))
    assert result.is_error
    assert result.error_code == ToolErrorCode.NETWORK_ERROR.value


async def test_tavily_search_output_truncation(monkeypatch) -> None:
    """超长 title/url/snippet 被截断，总长度不超过上限（开发计划 §2.5）。"""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    huge = "x" * 1000
    body = {
        "results": [
            {"title": huge, "url": huge, "content": huge, "score": 0.5}
            for _ in range(5)
        ]
    }
    client = FakeHttpClient([_ok(json.dumps(body))])
    result = await web_search(SearchArgs(query="python", max_results=5), _ctx(client))
    assert not result.is_error
    data = json.loads(result.output)
    for item in data["results"]:
        assert len(item["title"]) <= 200
        assert len(item["url"]) <= 500
        assert len(item["snippet"]) <= 300
    # 总输出不超过 _MAX_TOTAL（4000）字符。
    assert len(result.output) <= 4000


# ───────────────────────── Open-Meteo 天气 ─────────────────────────


def _geo_payload(results: list[dict]) -> str:
    return json.dumps({"results": results} if results else {})


def _fc_payload() -> str:
    return json.dumps(
        {
            "current": {"temperature_2m": 20.0, "weather_code": 1},
            "daily": {
                "time": ["2026-07-23"],
                "weather_code": [1],
                "temperature_2m_max": [25.0],
                "temperature_2m_min": [15.0],
                "precipitation_probability_max": [10],
            },
        }
    )


_UNIQUE = [
    {
        "name": "Beijing",
        "country": "China",
        "admin1": "Beijing",
        "latitude": 39.9,
        "longitude": 116.4,
        "timezone": "Asia/Shanghai",
    }
]


async def test_open_meteo_unique_candidate_returns_forecast(monkeypatch) -> None:
    """唯一候选直接返回预报结构（type=forecast）。"""
    client = FakeHttpClient([_ok(_geo_payload(_UNIQUE)), _ok(_fc_payload())])
    result = await get_forecast(WeatherArgs(location="Beijing"), _ctx(client))
    assert not result.is_error
    data = json.loads(result.output)
    assert data["type"] == "forecast"
    assert data["location"]["name"] == "Beijing"
    assert "current" in data and "daily" in data
    # 地理编码 + 预报两次请求；预报请求固定当前/每日字段。
    assert client.calls[0]["service"] == "open_meteo"
    assert client.calls[1]["service"] == "open_meteo"


async def test_open_meteo_zero_candidates_returns_no_candidate(monkeypatch) -> None:
    """零候选返回稳定的业务结构（type=no_candidate），不静默猜测。"""
    client = FakeHttpClient([_ok(_geo_payload([]))])
    result = await get_forecast(WeatherArgs(location="ZZZNotFound"), _ctx(client))
    assert not result.is_error
    data = json.loads(result.output)
    assert data["type"] == "no_candidate"
    assert data["location"] == "ZZZNotFound"


async def test_open_meteo_multi_candidate_returns_candidates(monkeypatch) -> None:
    """多候选返回至多 5 个候选，供 Agent 向用户追问。"""
    many = [
        {"name": f"City{i}", "country": "X", "admin1": "", "latitude": i, "longitude": i}
        for i in range(9)
    ]
    client = FakeHttpClient([_ok(_geo_payload(many))])
    result = await get_forecast(WeatherArgs(location="Springfield"), _ctx(client))
    assert not result.is_error
    data = json.loads(result.output)
    assert data["type"] == "candidates"
    # 至多 5 个候选。
    assert len(data["candidates"]) == 5


async def test_open_meteo_country_code_narrows(monkeypatch) -> None:
    """country_code 被拼入地理编码请求，缩小候选范围（阶段三验收）。"""
    client = FakeHttpClient([_ok(_geo_payload(_UNIQUE)), _ok(_fc_payload())])
    await get_forecast(WeatherArgs(location="Beijing", country_code="cn"), _ctx(client))
    geo_url = client.calls[0]["url"]
    assert "countryCode=CN" in geo_url


async def test_open_meteo_days_boundary(monkeypatch) -> None:
    """days=1 与 days=7 为合法边界；0 与 8 被 Pydantic 校验拒绝。"""
    # 合法边界应能通过校验并进入 Provider（forecast_days 正确传递）。
    client = FakeHttpClient([_ok(_geo_payload(_UNIQUE)), _ok(_fc_payload())])
    await get_forecast(WeatherArgs(location="Beijing", days=7), _ctx(client))
    assert "forecast_days=7" in client.calls[1]["url"]

    # 非法边界：Pydantic 校验在构造期拒绝。
    with pytest.raises(Exception):
        WeatherArgs(location="Beijing", days=0)
    with pytest.raises(Exception):
        WeatherArgs(location="Beijing", days=8)


async def test_open_meteo_forecast_failure_5xx(monkeypatch) -> None:
    """预报请求 5xx 映射为 NETWORK_ERROR。"""
    client = FakeHttpClient(
        [_ok(_geo_payload(_UNIQUE)), ProviderHttpResponse(status_code=500, text="{}", headers={})]
    )
    result = await get_forecast(WeatherArgs(location="Beijing"), _ctx(client))
    assert result.is_error
    assert result.error_code == ToolErrorCode.NETWORK_ERROR.value


async def test_open_meteo_geocode_failure_4xx(monkeypatch) -> None:
    """地理编码 4xx（非鉴权）映射为 NETWORK_ERROR（不重试）。

    注：401/403 按统一映射归为 CONFIGURATION_ERROR（鉴权失败），故此处用 404 验证
    普通 4xx → NETWORK_ERROR 的路径。
    """
    client = FakeHttpClient([ProviderHttpResponse(status_code=404, text="{}", headers={})])
    result = await get_forecast(WeatherArgs(location="Beijing"), _ctx(client))
    assert result.is_error
    assert result.error_code == ToolErrorCode.NETWORK_ERROR.value


async def test_open_meteo_passes_retry_once_flag(monkeypatch) -> None:
    """Open-Meteo 的地理编码请求向 HttpClient 传递 retry_once=True（临时错误重试由客户端负责）。

    实际的一次重试语义由 HttpxHttpClient 在 test_http_client.py 中通过 MockTransport 覆盖。
    """
    client = FakeHttpClient([ProviderHttpResponse(status_code=500, text="{}", headers={})])
    result = await get_forecast(WeatherArgs(location="Beijing"), _ctx(client))
    assert result.is_error
    assert result.error_code == ToolErrorCode.NETWORK_ERROR.value
    # 地理编码请求携带 retry_once=True；Tavily 对应的搜索请求则不传（见下方断言）。
    assert client.calls[0]["retry_once"] is True


async def test_tavily_does_not_request_retry(monkeypatch) -> None:
    """Tavily 为避免重复计费，搜索请求不传递 retry_once（由客户端保证不重试）。"""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    client = FakeHttpClient([_ok(json.dumps({"results": []}))])
    await web_search(SearchArgs(query="python"), _ctx(client))
    assert client.calls[0]["retry_once"] is False


# ───────────────── 执行器层：脱敏网络审计（开发计划 §2.6） ─────────────────


class FakeJournal:
    """记录 Journal 网络审计事件的最小替身（不含会话约束）。

    执行器链路还会调用 tool_start / tool_policy_resolved / tool_end 等事件，
    这里以空实现吞掉，仅聚焦断言 network_audit 的脱敏内容。
    """

    def __init__(self) -> None:
        self.network_audits: list[dict] = []

    def tool_start(self, *args, **kwargs) -> None:
        pass

    def tool_end(self, *args, **kwargs) -> None:
        pass

    def tool_policy_resolved(self, *args, **kwargs) -> None:
        pass

    def tool_approval_outcome(self, *args, **kwargs) -> None:
        pass

    def tool_network_audit(self, *, tool_name, service, host, status_class, elapsed_ms, bytes_len, retries) -> None:
        self.network_audits.append(
            {
                "tool_name": tool_name,
                "service": service,
                "host": host,
                "status_class": status_class,
                "elapsed_ms": elapsed_ms,
                "bytes_len": bytes_len,
                "retries": retries,
            }
        )


async def test_executor_emits_desensitized_network_audit(monkeypatch) -> None:
    """网络 Tool 经完整链路（Broker→Policy→Handler）执行后，Executor 写入脱敏 network_audit。

    审计仅含服务/主机/状态类别/耗时/字节/重试，不含密钥、认证头或 URL 查询串。
    """
    monkeypatch.setenv("TAVILY_API_KEY", "secret-key")
    cfg = Config()
    cfg.tools.network = NetworkToolsConfig(
        tavily=NetworkServiceConfig(enabled=True),
        open_meteo=NetworkServiceConfig(enabled=False),
    )
    client = FakeHttpClient([_ok(json.dumps({"results": [{"title": "T", "url": "https://e.com", "content": "C"}]}))])
    executor = _build_tools(cfg, None, client)
    journal = FakeJournal()
    ctx = ToolExecutionContext(agent_id="agent-1")

    outcome = await executor.execute(
        "builtin.web.search", {"query": "python"}, journal=journal, execution_context=ctx
    )
    assert not outcome.is_error
    assert len(journal.network_audits) == 1
    audit = journal.network_audits[0]
    assert audit["tool_name"] == "builtin.web.search"
    assert audit["service"] == "tavily"
    assert audit["host"] == "api.tavily.com"
    assert audit["status_class"] == "2xx"
    assert audit["retries"] == 0
    # 审计不含任何密钥/认证头线索。
    assert "secret-key" not in str(audit)
    assert "Bearer" not in str(audit)
