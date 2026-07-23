"""受控 HTTP 客户端（Tool v1 阶段二新增）。

提供一个仅供内置 Provider 使用的薄 ``HttpClient``：只暴露受限的异步请求接口，并在
自身再次校验服务、方法与完整 URL，形成纵深防御（开发计划 §2.4）。它不出现在 Agent
Tool Schema、Tool Registry 或用户提示词中；不直接依赖 Journal。

设计要点：
- 只允许 HTTPS、精确声明的固定主机与 443 端口；拒绝 IP 字面量、非标准端口、
  用户信息段、重定向与未声明路径的跨主机跳转（follow_redirects=False）。
- 默认连接超时 3 秒、总超时 10 秒、单响应最大 1 MiB、全局并发上限 4；这些值不暴露
  给 Agent。
- 用流式读取执行响应大小限制；不得先无界读取再检查长度。
- Tavily 不自动重试（避免重复计费）；Open-Meteo 仅对连接/读取超时等临时网络错误
  重试一次，HTTP 4xx 不重试（由调用方通过 retry_once 控制）。
- 不向 Provider 发送会话记录、系统提示词、工作区文件、环境变量或不属于 Tool 参数的
  内容；异常信息脱敏，不含密钥、认证头、完整 URL 查询串或响应正文。

所有新增注释使用中文。
"""

from __future__ import annotations

import asyncio
import json as json_module
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx

from .network import KNOWN_NETWORK_HOSTS, KNOWN_NETWORK_ROUTES

logger = logging.getLogger("dotclaw.tools.http_client")

# 资源边界常量（开发计划 §2.4），不暴露给 Agent。
_DEFAULT_CONNECT_TIMEOUT = 3.0
_DEFAULT_TOTAL_TIMEOUT = 10.0
_DEFAULT_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB
_DEFAULT_MAX_CONCURRENCY = 4

# 仅对以下临时网络错误在 retry_once 时重试一次；HTTP 4xx/5xx 由调用方映射，不重试。
_TRANSIENT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


class HttpClientError(Exception):
    """受控 HTTP 客户端的统一异常基类。

    所有异常消息均经过脱敏：不含密钥、认证头、完整 URL 查询串或响应正文，仅给出
    主机与错误类别，供上层安全映射为 NETWORK_ERROR / RESPONSE_TOO_LARGE。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ResponseTooLargeError(HttpClientError):
    """响应体超过最大允许大小（开发计划 §2.4）。"""


@dataclass
class ProviderHttpResponse:
    """Provider 视角的受限 HTTP 响应（不含原始连接对象）。

    status_code 用于 Provider 映射错误码；text 为已解码的响应体（已受大小限制）；
    headers 仅供必要解析（如 content-type），上层不应将其写入审计。elapsed_ms 与
    retries 由客户端填充，供脱敏 network_audit 摘要使用（开发计划 §2.6）。
    """

    status_code: int
    text: str
    headers: dict
    elapsed_ms: float = 0.0
    retries: int = 0


@runtime_checkable
class HttpClient(Protocol):
    """内置 Provider 依赖的窄 HTTP 客户端协议。

    仅暴露请求与关闭两个能力；具体实现（HttpxHttpClient）不得被 Agent 触及，
    Tool 注册表与用户提示词中也不出现此协议。
    """

    async def request(
        self,
        *,
        service: str,
        method: str,
        url: str,
        headers: dict | None = None,
        json: Any | None = None,
        retry_once: bool = False,
    ) -> "ProviderHttpResponse":
        """发起一次受控请求，返回受限响应。

        Args:
            service: Provider 服务标识（如 tavily / open_meteo），用于校验主机。
            method: HTTP 方法（如 POST / GET）。
            url: 完整 HTTPS URL；客户端再次校验其主机属于该 service 的声明主机。
            headers: 请求头（认证头由 Provider 构造，不含于日志/审计）。
            json: 请求体（dict）；客户端负责序列化并设置 Content-Type。
            retry_once: 是否对临时网络错误重试一次（仅 Open-Meteo 等安全场景）。
        """
        ...

    async def close(self) -> None:
        """关闭底层连接池（由 ApplicationHost 关闭流程调用）。"""
        ...


class HttpxHttpClient:
    """基于 httpx.AsyncClient 的受控 HTTP 客户端实现。

    构造期可注入 ``transport``（如 httpx.MockTransport）以便测试；生产环境使用默认
    传输并自动兼容系统 HTTP(S)_PROXY / NO_PROXY 环境变量（httpx trust_env 默认开启）。
    """

    def __init__(
        self,
        *,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        total_timeout: float = _DEFAULT_TOTAL_TIMEOUT,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
        transport: "httpx.AsyncBaseTransport | None" = None,
    ) -> None:
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=total_timeout,
            write=total_timeout,
            pool=total_timeout,
        )
        self._max_bytes = max_bytes
        self._sem = asyncio.Semaphore(max_concurrency)
        # follow_redirects=False：拒绝重定向（含跨主机跳转），由调用方处理 3xx。
        # trust_env=True：兼容系统已有的代理环境变量（开发计划 §1.3 实施假设）。
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=True,
            transport=transport,
        )
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
    ) -> "ProviderHttpResponse":
        """发起受控请求；在调用前再次校验服务/主机/端口，失败时抛出脱敏异常。"""
        if self._closed:
            raise HttpClientError("HTTP 客户端已关闭，无法发起请求")

        self._validate_url(service, method, url)
        content: bytes | None = None
        if json is not None:
            content = json_module.dumps(json, ensure_ascii=False).encode("utf-8")

        last_exc: Exception | None = None
        # retry_once 时重试一次（共两次尝试）；仅对临时网络错误重试。
        attempts = 0
        for _ in range(2 if retry_once else 1):
            attempts += 1
            try:
                async with self._sem:
                    start = time.perf_counter()
                    async with self._client.stream(
                        method, url, headers=headers, content=content
                    ) as resp:
                        body = await self._read_limited(resp)
                        # 用本地时钟测量耗时，避免依赖 httpx 流式响应下不可靠的
                        # resp.elapsed 属性（响应关闭后访问会抛 RuntimeError）。
                        elapsed_ms = (time.perf_counter() - start) * 1000
                        return ProviderHttpResponse(
                            status_code=resp.status_code,
                            text=body,
                            headers=dict(resp.headers),
                            elapsed_ms=elapsed_ms,
                            retries=attempts - 1,
                        )
            except httpx.HTTPError as exc:
                last_exc = exc
                if retry_once and isinstance(exc, _TRANSIENT_ERRORS):
                    logger.warning("服务 %s 临时网络错误，重试一次: %s", service, type(exc).__name__)
                    continue
                raise self._safe_error(exc, url) from exc

        # 重试耗尽（仅 retry_once 且均为临时错误时到达此处）。
        assert last_exc is not None
        raise self._safe_error(last_exc, url) from last_exc

    def _validate_url(self, service: str, method: str, url: str) -> None:
        """再次校验 URL：HTTPS、声明主机、443 端口、无用户信息段、固定路由。"""
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise HttpClientError("仅允许 HTTPS 请求")
        if parsed.port is not None and parsed.port != 443:
            raise HttpClientError("仅允许 443 端口的 HTTPS 请求")
        if parsed.username or parsed.password:
            # 拒绝包含用户信息的 URL（纵使不含真实密钥，亦属越权形态）。
            raise HttpClientError("URL 不得包含用户信息段")
        host = parsed.hostname or ""
        allowed_hosts = KNOWN_NETWORK_HOSTS.get(service, [])
        if host not in allowed_hosts:
            # 不回显完整 URL（可能含查询串），仅给出服务与主机。
            raise HttpClientError(f"服务 {service} 不允许访问主机 {host}")

        # 二次纵深防御：同一允许主机上，只有该服务声明的 (方法, 路径) 才能通过。
        allowed_routes = KNOWN_NETWORK_ROUTES.get(service, [])
        route = (method.upper(), parsed.path)
        if route not in allowed_routes:
            raise HttpClientError(
                f"服务 {service} 不允许 {method.upper()} {parsed.path or '/' }"
            )

    async def _read_limited(self, resp: "httpx.Response") -> str:
        """流式读取响应体并强制大小上限；超限立即抛脱敏异常。"""
        body = b""
        async for chunk in resp.aiter_bytes():
            body += chunk
            if len(body) > self._max_bytes:
                raise ResponseTooLargeError("响应体超过最大允许大小")
        return body.decode("utf-8", "replace")

    @staticmethod
    def _safe_error(exc: Exception, url: str) -> HttpClientError:
        """把 httpx 异常转换为脱敏的 HttpClientError。

        不回显查询串、认证头或响应体；仅给出主机与错误类别。
        """
        host = urlparse(url).hostname or "未知主机"
        return HttpClientError(f"网络请求失败（{host}）：{type(exc).__name__}")

    async def close(self) -> None:
        """关闭底层连接池。幂等；由 ApplicationHost 关闭流程调用。"""
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    @property
    def is_closed(self) -> bool:
        """是否已关闭（供关闭流程与测试断言）。"""
        return self._closed
