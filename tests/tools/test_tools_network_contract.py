"""Tool v1 网络能力契约测试（开发计划 阶段0 / 阶段1）。

覆盖固定 Provider 的静态网络能力链路，不涉及任何真实 HTTP 请求：
- Broker 从 ToolDefinition 的 network_service / network_hosts 静态声明生成请求，
  绝不读取 Agent 参数中的 URL。
- Policy 对 NETWORK_HTTP 请求执行 fail-closed：服务未启用、主机错配、全局拒绝、
  Agent 收窄均拒绝。
- 配置投影：启用服务派生 network.http=allow 并投影精确主机；禁用则 deny。
- 新增错误码映射（CONFIGURATION_ERROR / NETWORK_ERROR / RESPONSE_TOO_LARGE）。

所有新增注释使用中文。
"""

from __future__ import annotations

from dotclaw.tools.base import (
    ToolDefinition,
    ToolErrorCode,
    ToolErrorType,
    ToolExecutionContext,
    ToolResult,
)
from dotclaw.tools.capability import (
    CapabilityBroker,
    CapabilityRequest,
    ResourceKind,
)
from dotclaw.tools.decorator import ToolPolicy
from dotclaw.tools.policy import (
    PolicyDecision,
    PolicyEngine,
    PolicyScope,
    default_policy_scope,
)
from dotclaw.tools.network import KNOWN_NETWORK_HOSTS

from dotclaw.bootstrap._host_components import _build_tools
from dotclaw.config.settings import Config, NetworkServiceConfig, NetworkToolsConfig


# ── 工具定义构造辅助 ──

def _network_def(service: str, hosts: list[str]) -> ToolDefinition:
    """构造一个声明了静态网络能力的 ToolDefinition。"""
    return ToolDefinition(
        name=f"builtin.net.{service}",
        description="契约测试工具",
        policy_profile=ToolPolicy.NETWORK.value,
        network_service=service,
        network_hosts=hosts,
    )


def _scope_with_network(services: dict[str, list[str]], http: PolicyDecision) -> PolicyScope:
    """构造带网络服务约束与作用域。"""
    scope = default_policy_scope()
    scope.network_services = services
    scope.global_rules["network.http"] = http
    return scope


# ── Broker：静态声明解析 ──

def test_broker_single_host_from_static_declaration():
    """Broker 为单个静态主机生成一条 NETWORK_HTTP 请求，携带 service 与 host。"""
    definition = _network_def("tavily", ["api.tavily.com"])
    requests = CapabilityBroker().resolve(definition, {}, workspace_root=".")

    assert len(requests) == 1
    req = requests[0]
    assert req.kind is ResourceKind.NETWORK_HTTP
    assert req.profile == ToolPolicy.NETWORK.value
    assert req.service == "tavily"
    assert req.host == "api.tavily.com"


def test_broker_multi_host_generates_multiple_requests():
    """Open-Meteo 声明两个精确主机，Broker 各生成一条请求。"""
    hosts = KNOWN_NETWORK_HOSTS["open_meteo"]
    definition = _network_def("open_meteo", hosts)
    requests = CapabilityBroker().resolve(definition, {}, workspace_root=".")

    assert len(requests) == len(hosts)
    assert {r.host for r in requests} == set(hosts)
    assert all(r.service == "open_meteo" for r in requests)


def test_broker_ignores_agent_url_param():
    """Broker 不读取 Agent 参数中的 url——恶意 url 不影响生成的主机。"""
    definition = _network_def("tavily", ["api.tavily.com"])
    malicious_args = {"query": "x", "url": "http://evil.example.com/secret"}
    requests = CapabilityBroker().resolve(definition, malicious_args, workspace_root=".")

    assert len(requests) == 1
    assert requests[0].host == "api.tavily.com"


def test_broker_network_without_declaration_still_emits_request():
    """声明 NETWORK 档案却缺少 service/hosts 时仍生成一条请求（fail-closed 兜底）。"""
    definition = ToolDefinition(
        name="builtin.net.broken",
        description="",
        policy_profile=ToolPolicy.NETWORK.value,
        network_service=None,
        network_hosts=[],
    )
    requests = CapabilityBroker().resolve(definition, {}, workspace_root=".")
    assert len(requests) == 1
    assert requests[0].service is None
    assert requests[0].host is None


# ── Policy：fail-closed 组合 ──

def test_policy_network_default_deny():
    """未启用任何服务：network.http 默认 deny，请求被拒。"""
    definition = _network_def("tavily", ["api.tavily.com"])
    requests = CapabilityBroker().resolve(definition, {}, workspace_root=".")
    scope = _scope_with_network(services={}, http=PolicyDecision.DENY)

    outcome = PolicyEngine(scope).evaluate(requests, scope)
    assert outcome.decision is PolicyDecision.DENY


def test_policy_network_service_disabled_denies():
    """服务未启用（network_services 为空）即使全局 allow 也拒绝。"""
    definition = _network_def("tavily", ["api.tavily.com"])
    requests = CapabilityBroker().resolve(definition, {}, workspace_root=".")
    scope = _scope_with_network(services={}, http=PolicyDecision.ALLOW)

    outcome = PolicyEngine(scope).evaluate(requests, scope)
    assert outcome.decision is PolicyDecision.DENY


def test_policy_network_host_mismatch_denies():
    """服务已启用，但请求主机不在允许列表 → 拒绝。"""
    definition = _network_def("tavily", ["evil.example.com"])
    requests = CapabilityBroker().resolve(definition, {}, workspace_root=".")
    scope = _scope_with_network(
        services={"tavily": ["api.tavily.com"]}, http=PolicyDecision.ALLOW
    )

    outcome = PolicyEngine(scope).evaluate(requests, scope)
    assert outcome.decision is PolicyDecision.DENY


def test_policy_network_allowed_host_passes():
    """服务已启用且主机精确匹配 → 允许。"""
    definition = _network_def("tavily", ["api.tavily.com"])
    requests = CapabilityBroker().resolve(definition, {}, workspace_root=".")
    scope = _scope_with_network(
        services={"tavily": ["api.tavily.com"]}, http=PolicyDecision.ALLOW
    )

    outcome = PolicyEngine(scope).evaluate(requests, scope)
    assert outcome.decision is PolicyDecision.ALLOW


def test_policy_network_agent_narrowing_overrides_allow():
    """Agent 策略收窄为 deny 时，即使全局 allow 且服务启用也拒绝。"""
    definition = _network_def("tavily", ["api.tavily.com"])
    requests = CapabilityBroker().resolve(definition, {}, workspace_root=".")
    scope = _scope_with_network(
        services={"tavily": ["api.tavily.com"]}, http=PolicyDecision.ALLOW
    )
    scope.agent_rules["network.http"] = PolicyDecision.DENY

    outcome = PolicyEngine(scope).evaluate(requests, scope)
    assert outcome.decision is PolicyDecision.DENY


def test_policy_network_agent_ask_narrowing_blocks_without_channel():
    """Agent 收窄为 ask 且无交互通道时拒绝（不静默放行）。"""
    definition = _network_def("tavily", ["api.tavily.com"])
    requests = CapabilityBroker().resolve(definition, {}, workspace_root=".")
    scope = _scope_with_network(
        services={"tavily": ["api.tavily.com"]}, http=PolicyDecision.ALLOW
    )
    scope.agent_rules["network.http"] = PolicyDecision.ASK

    outcome = PolicyEngine(scope).evaluate(requests, scope)
    assert outcome.decision is PolicyDecision.ASK


# ── 配置投影：network.http 派生 ──

def test_config_derivation_all_disabled():
    """默认配置（两服务均关闭）→ network_services 为空且 network.http=deny。"""
    cfg = Config()
    assert cfg.tools.network.tavily.enabled is False
    assert cfg.tools.network.open_meteo.enabled is False

    executor = _build_tools(cfg, None)
    scope = executor.policy_engine.scope
    assert scope.network_services == {}
    assert scope.global_rules["network.http"] is PolicyDecision.DENY


def test_config_derivation_tavily_enabled():
    """启用 tavily → 投影精确主机且 network.http 派生为 allow。"""
    cfg = Config()
    cfg.tools.network = NetworkToolsConfig(
        tavily=NetworkServiceConfig(enabled=True),
        open_meteo=NetworkServiceConfig(enabled=False),
    )
    executor = _build_tools(cfg, None)
    scope = executor.policy_engine.scope

    assert scope.network_services == {"tavily": KNOWN_NETWORK_HOSTS["tavily"]}
    assert scope.global_rules["network.http"] is PolicyDecision.ALLOW


def test_config_explicit_network_http_priority():
    """用户显式写出 network.http 时优先于派生逻辑。"""
    cfg = Config()
    cfg.tools.network = NetworkToolsConfig(
        tavily=NetworkServiceConfig(enabled=True),
    )
    # 显式 deny 即使启用服务也保持 deny。
    cfg.tools.policy.rules["network.http"] = "deny"
    executor = _build_tools(cfg, None)
    scope = executor.policy_engine.scope

    assert scope.network_services == {"tavily": KNOWN_NETWORK_HOSTS["tavily"]}
    assert scope.global_rules["network.http"] is PolicyDecision.DENY


# ── 新增错误码映射 ──

def test_new_error_codes_map_to_types():
    """CONFIGURATION_ERROR / NETWORK_ERROR / RESPONSE_TOO_LARGE 映射正确类型。"""
    for code, expected_type in (
        (ToolErrorCode.CONFIGURATION_ERROR, ToolErrorType.CONFIGURATION),
        (ToolErrorCode.NETWORK_ERROR, ToolErrorType.NETWORK),
        (ToolErrorCode.RESPONSE_TOO_LARGE, ToolErrorType.RESPONSE_TOO_LARGE),
    ):
        result = ToolResult.from_error(code=code, message="x")
        assert result.is_error
        assert result.error_code == code.value
        assert result.error_type == expected_type.value


def test_tool_definition_backward_compatible_defaults():
    """未提供网络字段时 ToolDefinition 仍可正常构造（不影响既有 builtin / MCP）。"""
    # 既有 builtin 工具通常只声明 name/description/parameters。
    legacy = ToolDefinition(name="builtin.system.get_time", description="t")
    assert legacy.network_service is None
    assert legacy.network_hosts == []
    # ToolExecutionContext 的内部 http_client 注入槽默认 None（阶段二装配）。
    ctx = ToolExecutionContext()
    assert ctx.http_client is None
