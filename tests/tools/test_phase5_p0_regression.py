"""阶段五审计 P0 漏洞回归测试（三类阻断问题）。

覆盖：
- P0-1：`build_agent` 启用 MCP 时 `_build_mcp` 引用未定义 `tool_executor` 导致
  NameError（旧签名 `(config, tool_registry)` 内部引用 `tool_executor`）。
- P0-2a：文件安全策略路径逃逸检测未展开 `~`，导致 `~/.ssh/id_rsa` 等被误判为
  工作区内路径。
- P0-2b：memory 工具参数名 `long_term_file` 未被 Broker 读取，任意记忆路径被误判
  为工作区根目录 `.`。
- P0-3：`mcp.connect: ask` 在无交互审批通道时等同拒绝（fail-closed），不在
  allowed_mcp_servers 的 server 必须被拒绝。

所有新增注释使用中文。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from dotclaw.bootstrap._host_components import _build_mcp
from dotclaw.mcp.provider import MCPToolProvider
from dotclaw.tools.approval import ApprovalManager
from dotclaw.tools.capability import (
    CapabilityBroker,
    CapabilityRequest,
    ResourceKind,
    normalize_workspace_path,
)
from dotclaw.tools.decorator import ToolPolicy, get_tool_meta, tool
from dotclaw.tools.executor import ToolExecutor
from dotclaw.tools.function_handler import FunctionToolHandler
from dotclaw.tools.policy import PolicyDecision, PolicyEngine, default_policy_scope
from dotclaw.tools.registry import ToolRegistry


# ============================================================================
# 工具构造辅助
# ============================================================================

class LongTermArgs(BaseModel):
    long_term_file: str = "."


@tool(
    name="mem.read",
    description="读记忆（回归用镜像）",
    policy=ToolPolicy.WORKSPACE_READ,
    path_param="long_term_file",
    args_model=LongTermArgs,
)
async def mem_read(args, context):
    return "ok"


def _defn(func):
    return FunctionToolHandler(func, get_tool_meta(func)).definition()


def _make_executor() -> ToolExecutor:
    """构造最小可用 ToolExecutor（不依赖 config.yaml / builtin 发现）。"""
    registry = ToolRegistry()
    scope = default_policy_scope()
    policy_engine = PolicyEngine(scope)
    capability_broker = CapabilityBroker()
    approval_mgr = ApprovalManager()
    return ToolExecutor(
        registry=registry,
        approval_manager=approval_mgr,
        policy_engine=policy_engine,
        capability_broker=capability_broker,
        skill_parser=None,
        approval_commands=set(),
    )


# ============================================================================
# P0-1：`_build_mcp` 不得引用未定义变量
# ============================================================================

class _FakeServerConfig:
    def __init__(self, name: str):
        self.name = name


class _FakeMCPProvider:
    """记录在 `_build_mcp` 中实际传入的安全组件引用，避免真实网络副作用。"""

    instances: list[dict] = []

    def __init__(self, *, global_config, server_configs, registry, policy_engine, capability_broker, **kwargs):
        _FakeMCPProvider.instances.append(
            {
                "registry": registry,
                "policy_engine": policy_engine,
                "capability_broker": capability_broker,
            }
        )

    async def start(self):
        return []


def test_build_mcp_reuses_tool_executor_components():
    """P0-1 回归：`_build_mcp` 必须接收 `tool_executor` 并复用其 registry /
    policy_engine / capability_broker。旧实现签名 `(config, tool_registry)` 且内部
    引用未定义的 `tool_executor`，会抛 `NameError: name 'tool_executor' is not
    defined`。"""
    executor = _make_executor()
    config = SimpleNamespace(
        tools=SimpleNamespace(
            mcp_enabled=True,
            mcp_servers=[_FakeServerConfig("github")],
            mcp_global=None,
        )
    )

    captured: list[dict] = []
    _FakeMCPProvider.instances = captured

    async def _run():
        # 旧代码：`_build_mcp(config, tool_registry)` 中 `tool_executor` 未定义 → NameError。
        # 修复后：`_build_mcp(config, tool_executor)` 复用 executor 的安全组件，并在启动时
        # 完成首次发现（启动就绪语义），直接返回已就绪的 Provider。
        provider = await _build_mcp(config, executor)
        return provider

    with patch("dotclaw.mcp.MCPToolProvider", _FakeMCPProvider):
        provider = asyncio.run(_run())

    assert provider is not None
    assert captured, "MCPToolProvider 应被构造"
    last = captured[-1]
    # 必须复用同一个 ToolExecutor 实例的安全组件（引用相等）。
    assert last["registry"] is executor.registry
    assert last["policy_engine"] is executor.policy_engine
    assert last["capability_broker"] is executor.capability_broker


# ============================================================================
# P0-2a：`~` 展开后路径逃逸检测
# ============================================================================

def test_normalize_tilde_escapes_workspace_root():
    """P0-2a 回归：`normalize_workspace_path` 必须先 expanduser 再 realpath，否则
    `~/.ssh/id_rsa` 被当作工作区内字面路径 `~/.ssh/id_rsa`（不逃逸）。修复后应判定
    为逃逸 workspace 根目录。"""
    with tempfile.TemporaryDirectory() as root:
        normalized, escaped = normalize_workspace_path(root, "~/.ssh/id_rsa")
        assert escaped is True
        # 逃逸回退为绝对路径（真实用户主目录下的文件）。
        assert os.path.isabs(normalized.replace("/", os.sep))


# ============================================================================
# P0-2b：memory 工具 path_param 读取
# ============================================================================

def test_memory_path_param_detects_escape_outside_workspace():
    """P0-2b 回归：memory 工具参数名为 `long_term_file`（非默认 `path`），Broker 必须
    读取该参数做逃逸检测。工作区外的绝对路径应判定为逃逸。"""
    broker = CapabilityBroker()
    with tempfile.TemporaryDirectory() as root:
        outside = os.path.abspath(os.path.join(root, "..", "outside_mem.md"))
        reqs = broker.resolve(_defn(mem_read), LongTermArgs(long_term_file=outside), root)
        assert len(reqs) == 1
        assert reqs[0].kind is ResourceKind.FILE_READ
        assert reqs[0].escaped is True


def test_memory_path_param_allows_inside_workspace():
    """对照：工作区内的相对记忆路径不应被判定为逃逸。"""
    broker = CapabilityBroker()
    with tempfile.TemporaryDirectory() as root:
        reqs = broker.resolve(_defn(mem_read), LongTermArgs(long_term_file="notes/mem.md"), root)
        assert len(reqs) == 1
        assert reqs[0].escaped is False


def test_real_memory_tool_declares_path_param():
    """真实 `builtin.memory.read` 必须声明 `path_param="long_term_file"`，
    否则 Broker 读不到 `long_term_file` 字段，任意记忆路径会被误判为工作区根目录。"""
    from dotclaw.tools.builtin.memory_tool import read as memory_read

    meta = get_tool_meta(memory_read)
    assert meta.path_param == "long_term_file"


# ============================================================================
# P0-3：mcp.connect fail-closed
# ============================================================================

def test_mcp_connect_fail_closed_when_server_not_allowed():
    """P0-3 回归：`mcp.connect` 默认 `ask`，但连接发生在后台、无交互通道，server 不在
    allowed_mcp_servers 时必须 DENY（fail-closed）。空列表视为 deny-all。"""
    scope = default_policy_scope()
    scope.allowed_mcp_servers = []  # deny-all
    engine = PolicyEngine(scope)
    request = CapabilityRequest(kind=ResourceKind.MCP_CONNECT, profile="mcp.connect", server="evil")
    outcome = engine.evaluate([request])
    assert outcome.decision is PolicyDecision.DENY


def test_mcp_connect_allowed_when_in_allowed_list():
    """对照：server 显式位于 allowed_mcp_servers（配置预授权）时 ALLOW，免交互连接。"""
    scope = default_policy_scope()
    scope.allowed_mcp_servers = ["github"]
    engine = PolicyEngine(scope)
    request = CapabilityRequest(kind=ResourceKind.MCP_CONNECT, profile="mcp.connect", server="github")
    outcome = engine.evaluate([request])
    assert outcome.decision is PolicyDecision.ALLOW


def test_provider_authorize_connect_denies_unlisted_server():
    """Provider 级：`_authorize_connect` 对不在允许列表的 server 返回拒绝原因
    （非 None），不发起任何连接。"""
    scope = default_policy_scope()
    scope.allowed_mcp_servers = ["github"]
    policy = PolicyEngine(scope)
    provider = MCPToolProvider(
        global_config=None,
        server_configs=[_FakeServerConfig("evil")],
        registry=ToolRegistry(),
        policy_engine=policy,
    )
    reason = provider._authorize_connect("evil")
    assert reason is not None
    assert "未明确允许" in reason


def test_provider_authorize_connect_allows_listed_server():
    """Provider 级对照：在允许列表的 server 返回 None（放行）。"""
    scope = default_policy_scope()
    scope.allowed_mcp_servers = ["github"]
    policy = PolicyEngine(scope)
    provider = MCPToolProvider(
        global_config=None,
        server_configs=[_FakeServerConfig("github")],
        registry=ToolRegistry(),
        policy_engine=policy,
    )
    assert provider._authorize_connect("github") is None
