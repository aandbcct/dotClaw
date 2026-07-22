"""阶段四：Policy 对 MCP 的 mcp.connect / mcp.call 网关测试。"""

from __future__ import annotations

from dotclaw.tools.capability import CapabilityRequest, ResourceKind
from dotclaw.tools.policy import PolicyEngine, PolicyDecision, default_policy_scope


def _engine(allowed=None):
    scope = default_policy_scope()
    if allowed is not None:
        scope.allowed_mcp_servers = allowed
    return PolicyEngine(scope)


def test_mcp_call_allowed_server_passes():
    engine = _engine(allowed=["github"])
    out = engine.evaluate([CapabilityRequest(ResourceKind.MCP_CALL, "mcp.call", server="github")])
    # 默认 mcp.call=ask → 需要审批，但不是拒绝。
    assert out.decision is PolicyDecision.ASK


def test_mcp_call_disallowed_server_denied():
    engine = _engine(allowed=["github"])
    out = engine.evaluate([CapabilityRequest(ResourceKind.MCP_CALL, "mcp.call", server="evil")])
    assert out.decision is PolicyDecision.DENY
    assert "不在允许列表" in out.reason


def test_mcp_connect_allowed_server_passes():
    engine = _engine(allowed=["github"])
    out = engine.evaluate([CapabilityRequest(ResourceKind.MCP_CONNECT, "mcp.connect", server="github")])
    assert out.decision is not PolicyDecision.DENY


def test_mcp_connect_disallowed_server_denied():
    engine = _engine(allowed=["github"])
    out = engine.evaluate([CapabilityRequest(ResourceKind.MCP_CONNECT, "mcp.connect", server="evil")])
    assert out.decision is PolicyDecision.DENY
    assert "不在允许列表" in out.reason


def test_mcp_call_deny_blocks_even_if_other_allow():
    engine = _engine(allowed=["github"])
    requests = [
        CapabilityRequest(ResourceKind.MCP_CALL, "mcp.call", server="github"),
        CapabilityRequest(ResourceKind.MCP_CALL, "mcp.call", server="evil"),
    ]
    out = engine.evaluate(requests)
    assert out.decision is PolicyDecision.DENY


def test_empty_allow_list_denies_all_mcp_calls():
    # allowed_mcp_servers 为空且非 None → 任意 server 均拒绝。
    engine = _engine(allowed=[])
    out = engine.evaluate([CapabilityRequest(ResourceKind.MCP_CALL, "mcp.call", server="github")])
    assert out.decision is PolicyDecision.DENY
