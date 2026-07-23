"""Provider 公共基础（Tool v1 阶段三新增）。

集中定义 Provider 统一的异常与 HTTP 状态→统一错误码映射，供各 Provider 复用，
保证“不泄密”的错误信息语义一致（开发计划 §2.6）。所有新增注释使用中文。
"""

from __future__ import annotations

from dotclaw.tools.base import ToolErrorCode
from dotclaw.tools.http_client import HttpClientError


class ProviderError(Exception):
    """Provider 层的统一异常，携带应映射到的统一工具错误码。

    Provider 不直接构造 ToolResult，而是抛出带 ``code`` 的 ProviderError，由内置
    Tool 的 handler 统一映射为脱敏的 ToolResult（开发计划 §2.6）。
    """

    def __init__(self, code: ToolErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


def map_http_status(service_label: str, status_code: int) -> ProviderError:
    """把 HTTP 状态码映射为脱敏的 ProviderError（开发计划 §2.6）。

    错误信息不含密钥、认证头、完整 URL 查询串或响应正文，仅给出服务标签与状态类别。
    """
    if status_code in (401, 403):
        # 鉴权失败多因缺失/无效 Key，归为配置类错误。
        return ProviderError(
            ToolErrorCode.CONFIGURATION_ERROR,
            f"{service_label}鉴权失败（请检查对应的 API Key 环境变量）",
        )
    if status_code == 429:
        return ProviderError(ToolErrorCode.NETWORK_ERROR, f"{service_label}限流，请稍后重试")
    if 400 <= status_code < 500:
        return ProviderError(
            ToolErrorCode.NETWORK_ERROR, f"{service_label}请求被拒绝（状态码 {status_code}）"
        )
    if status_code >= 500:
        return ProviderError(
            ToolErrorCode.NETWORK_ERROR, f"{service_label}服务暂时不可用（状态码 {status_code}）"
        )
    return ProviderError(
        ToolErrorCode.NETWORK_ERROR, f"{service_label}返回异常状态码 {status_code}"
    )


async def call(
    client: "object",
    *,
    service: str,
    method: str,
    url: str,
    label: str,
    headers: dict | None = None,
    json: object | None = None,
    retry_once: bool = False,
) -> "object":
    """调用受控 HttpClient，并把脱敏的 HttpClientError 转换为统一 NETWORK_ERROR。

    这样超时/连接失败等网络异常在 Tool 层统一映射为 NETWORK_ERROR，而非
    EXECUTION_ERROR（开发计划 §2.6）。
    """
    try:
        # client 为 HttpClient 协议实现（HttpxHttpClient 或测试 Fake）。
        return await client.request(  # type: ignore[attr-defined]
            service=service,
            method=method,
            url=url,
            headers=headers,
            json=json,
            retry_once=retry_once,
        )
    except HttpClientError as exc:
        raise ProviderError(ToolErrorCode.NETWORK_ERROR, f"{label}网络请求失败") from exc
