"""ToolsConfig.policy 解析测试（Tool v1 阶段三）。

覆盖：
- _parse_tool_policy 缺省值
- 合法规则（allow/ask/deny 小写归一）、denied_paths、allowed_mcp_servers
- 非法决策值被忽略（避免悄悄放行/拒绝）
- _raw_to_config 把 tools.policy 装配进 ToolsConfig.policy
- tools.policy 缺失时回退默认 ToolPolicyConfig
所有新增注释使用中文。
"""

from __future__ import annotations

from dotclaw.config.settings import ToolPolicyConfig, _parse_tool_policy, _raw_to_config


def test_parse_tool_policy_defaults():
    cfg = _parse_tool_policy({})
    assert isinstance(cfg, ToolPolicyConfig)
    assert cfg.workspace_root == "."
    assert cfg.rules == {}
    assert cfg.denied_paths == []
    assert cfg.allowed_mcp_servers == []


def test_parse_tool_policy_accepts_valid_rules():
    cfg = _parse_tool_policy({
        "workspace_root": "/ws",
        "rules": {"workspace.read": "DENY", "network.http": "ask"},
        "denied_paths": [".env", "**/*.key"],
        "allowed_mcp_servers": ["github", "gitlab"],
    })
    assert cfg.workspace_root == "/ws"
    assert cfg.rules == {"workspace.read": "deny", "network.http": "ask"}
    assert cfg.denied_paths == [".env", "**/*.key"]
    assert cfg.allowed_mcp_servers == ["github", "gitlab"]


def test_parse_tool_policy_ignores_invalid_decision():
    # 非法决策值被忽略（告警），不应写入 rules（避免悄悄放行/拒绝）。
    cfg = _parse_tool_policy({"rules": {"workspace.read": "maybe"}})
    assert "workspace.read" not in cfg.rules


def test_raw_to_config_wires_policy():
    cfg = _raw_to_config({
        "tools": {
            "policy": {
                "workspace_root": "/data",
                "rules": {"workspace.write": "deny"},
                "denied_paths": [".secret"],
                "allowed_mcp_servers": ["github"],
            }
        }
    })
    p = cfg.tools.policy
    assert p.workspace_root == "/data"
    assert p.rules == {"workspace.write": "deny"}
    assert p.denied_paths == [".secret"]
    assert p.allowed_mcp_servers == ["github"]


def test_raw_to_config_policy_absent_uses_default():
    cfg = _raw_to_config({})
    assert isinstance(cfg.tools.policy, ToolPolicyConfig)
    assert cfg.tools.policy.workspace_root == "."
