"""固定网络服务路由常量（Tool v1 阶段一新增）。

集中声明受支持的网络 Provider 及其精确 HTTPS 主机，供 Policy 作用域投影（阶段一）
与受控 HTTP 客户端路由表（阶段二）复用，避免主机名单在多处重复、漂移。

所有新增注释使用中文。
"""

from __future__ import annotations

# 固定 Provider 服务标识 → 该服务允许被调用的精确 HTTPS 主机集合。
# 这些主机是代码级事实：不来自配置、Agent 参数或 YAML（开发计划 §2.2 / §2.4）。
KNOWN_NETWORK_HOSTS: dict[str, list[str]] = {
    "tavily": ["api.tavily.com"],
    "open_meteo": ["geocoding-api.open-meteo.com", "api.open-meteo.com"],
}
