"""Policy Engine 测试（Tool v1 阶段三）。

覆盖（总体设计 §4.3 / §10.1）：
- 默认规则 6 项
- passthrough（无请求）视为 ALLOW
- 各档案默认决策（read=allow / write=ask / exec=ask / net=deny / mcp=ask）
- Agent 只能收窄不能放宽（取更严格者）
- 资源约束：workspace 逃逸 → DENY；denied_paths 命中 → DENY；
  MCP server 不在允许列表 → DENY
- 多请求合并：任一 DENY → DENY；否则任一 ASK → ASK；否则 ALLOW
所有新增注释使用中文。
"""

from __future__ import annotations

from dotclaw.tools.capability import CapabilityRequest, ResourceKind
from dotclaw.tools.policy import (
    PolicyDecision,
    PolicyEngine,
    PolicyScope,
    default_policy_scope,
)


def _scope(global_rules=None, agent_rules=None, denied_paths=None, allowed=None, workspace_root="."):
    return PolicyScope(
        global_rules=global_rules or dict(default_policy_scope().global_rules),
        agent_rules=agent_rules or {},
        workspace_root=workspace_root,
        denied_paths=denied_paths or [],
        allowed_mcp_servers=allowed or [],
    )


def _req(kind, profile, **kw):
    return CapabilityRequest(kind=kind, profile=profile, **kw)


def test_default_rules_have_six_entries():
    rules = default_policy_scope().global_rules
    assert rules == {
        "workspace.read": PolicyDecision.ALLOW,
        "workspace.write": PolicyDecision.ASK,
        "process.exec": PolicyDecision.ASK,
        "network.http": PolicyDecision.DENY,
        "mcp.connect": PolicyDecision.ASK,
        "mcp.call": PolicyDecision.ASK,
    }


def test_passthrough_no_requests_is_allow():
    out = PolicyEngine().evaluate([])
    assert out.decision is PolicyDecision.ALLOW
    assert out.matched_rule == "none"


def test_workspace_read_default_allow():
    out = PolicyEngine().evaluate([_req(ResourceKind.FILE_READ, "workspace.read", normalized_path="a.txt")])
    assert out.decision is PolicyDecision.ALLOW


def test_network_default_deny():
    out = PolicyEngine().evaluate([_req(ResourceKind.NETWORK_HTTP, "network.http", host="x.com")])
    assert out.decision is PolicyDecision.DENY
    assert out.reason  # 拒绝须有原因，且不带用户输入值


def test_workspace_escape_is_deny_regardless_of_profile():
    out = PolicyEngine().evaluate(
        [_req(ResourceKind.FILE_READ, "workspace.read", normalized_path="/outside", escaped=True)]
    )
    assert out.decision is PolicyDecision.DENY
    assert "逃逸" in out.reason


def test_denied_paths_glob_matches():
    scope = _scope(denied_paths=["**/*.key"])
    out = PolicyEngine(scope).evaluate(
        [_req(ResourceKind.FILE_READ, "workspace.read", normalized_path="secret.key")]
    )
    assert out.decision is PolicyDecision.DENY
    assert "拒绝路径" in out.reason


def test_denied_paths_does_not_match_normal_file():
    scope = _scope(denied_paths=["**/*.key"])
    out = PolicyEngine(scope).evaluate(
        [_req(ResourceKind.FILE_READ, "workspace.read", normalized_path="notes.txt")]
    )
    assert out.decision is not PolicyDecision.DENY


def test_mcp_server_allowed_passes():
    scope = _scope(allowed=["github"])
    out = PolicyEngine(scope).evaluate([_req(ResourceKind.MCP_CALL, "mcp.call", server="github")])
    assert out.decision is not PolicyDecision.DENY


def test_mcp_server_not_allowed_is_deny():
    scope = _scope(allowed=["github"])
    out = PolicyEngine(scope).evaluate([_req(ResourceKind.MCP_CALL, "mcp.call", server="evil")])
    assert out.decision is PolicyDecision.DENY
    assert "不在允许列表" in out.reason


def test_agent_can_narrow_allow_to_deny():
    out = PolicyEngine(_scope(agent_rules={"workspace.read": PolicyDecision.DENY})).evaluate(
        [_req(ResourceKind.FILE_READ, "workspace.read", normalized_path="a.txt")]
    )
    assert out.decision is PolicyDecision.DENY


def test_agent_cannot_widen_deny_to_allow():
    # 全局 DENY（network）时，Agent 改写为 allow 不应生效，仍取更严格的 DENY。
    out = PolicyEngine(_scope(agent_rules={"network.http": PolicyDecision.ALLOW})).evaluate(
        [_req(ResourceKind.NETWORK_HTTP, "network.http", host="x.com")]
    )
    assert out.decision is PolicyDecision.DENY


def test_agent_cannot_widen_ask_to_allow():
    # 全局 ASK（write）时，Agent 改写为 allow 不应生效，仍取更严格的 ASK。
    out = PolicyEngine(_scope(agent_rules={"workspace.write": PolicyDecision.ALLOW})).evaluate(
        [_req(ResourceKind.FILE_WRITE, "workspace.write", normalized_path="a.txt")]
    )
    assert out.decision is PolicyDecision.ASK


def test_merge_any_deny_wins():
    out = PolicyEngine().evaluate([
        _req(ResourceKind.FILE_READ, "workspace.read", normalized_path="a.txt"),
        _req(ResourceKind.NETWORK_HTTP, "network.http", host="x.com"),  # DENY
    ])
    assert out.decision is PolicyDecision.DENY


def test_merge_ask_over_allow():
    out = PolicyEngine().evaluate([
        _req(ResourceKind.FILE_READ, "workspace.read", normalized_path="a.txt"),
        _req(ResourceKind.FILE_WRITE, "workspace.write", normalized_path="b.txt"),  # ASK
    ])
    assert out.decision is PolicyDecision.ASK


def test_merge_all_allow():
    out = PolicyEngine().evaluate([
        _req(ResourceKind.FILE_READ, "workspace.read", normalized_path="a.txt"),
        _req(ResourceKind.FILE_READ, "workspace.read", normalized_path="b.txt"),
    ])
    assert out.decision is PolicyDecision.ALLOW
