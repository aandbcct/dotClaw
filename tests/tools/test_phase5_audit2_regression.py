"""阶段五二次审计 P0 / P1 漏洞回归测试。

覆盖：
- P0：自定义 workspace_root 时，Broker 检查目标与实际文件操作目标必须一致。
  旧实现：Broker 在 workspace_root 下批准 relative.txt，但 handler 用进程 CWD 解析
  相对路径，安全边界在 Broker 批准、实际落到错误位置时失效。修复后 Executor 将
  Broker 校验过的绝对路径回填给 handler，落点与策略检查目标严格一致。
- P1：Agent 级策略必须按每个 Run / Invocation 冻结，而非保存在全局 ToolExecutor。
  旧实现：主 Agent 的 policy_rules 写入全局 scope，delegation 子 Agent 不会切换策略
  （子 Agent 收窄规则不生效，或主 Agent 规则污染所有 Agent）。修复后 Executor 按
  execution_context.agent_id 解析该 Agent 的 policy_rules 并构造独立作用域。
- 四次审计补充：基础 PolicyScope 只保留全局上限，主 Agent 规则不再注入共享 scope；
  子 Agent 无 policy_rules 时返回独立空 agent_rules 作用域，不继承主 Agent 的收窄规则。

所有新增注释使用中文。
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from dotclaw.tools.approval import ApprovalManager
from dotclaw.tools.capability import CapabilityBroker, ResourceKind
from dotclaw.tools.base import ToolExecutionContext
from dotclaw.tools.discovery import ToolDiscovery
from dotclaw.tools.executor import ToolExecutor
from dotclaw.tools.policy import PolicyDecision, PolicyEngine, default_policy_scope
from dotclaw.tools.registry import ToolRegistry


def _builtin_registry() -> ToolRegistry:
    """注册全部 builtin 工具（含文件读写、memory），便于端到端执行。"""
    registry = ToolRegistry()
    for handler in ToolDiscovery.discover_builtin():
        registry.register(handler)
    return registry


def _make_executor_with_root(workspace_root: str) -> ToolExecutor:
    """构造使用自定义 workspace_root 的 ToolExecutor（不依赖 config.yaml）。"""
    scope = default_policy_scope(workspace_root=workspace_root)
    policy_engine = PolicyEngine(scope)
    return ToolExecutor(
        registry=_builtin_registry(),
        approval_manager=ApprovalManager(),
        policy_engine=policy_engine,
        capability_broker=CapabilityBroker(),
        skill_parser=None,
        approval_commands=set(),
    )


def _make_executor_with_resolver(resolver) -> ToolExecutor:
    """构造带 Agent 级策略解析器的 ToolExecutor（全局 scope 不含 agent_rules）。"""
    scope = default_policy_scope()
    policy_engine = PolicyEngine(scope)
    return ToolExecutor(
        registry=_builtin_registry(),
        approval_manager=ApprovalManager(),
        policy_engine=policy_engine,
        capability_broker=CapabilityBroker(),
        skill_parser=None,
        approval_commands=set(),
        agent_policy_resolver=resolver,
    )


# ============================================================================
# P0：自定义 workspace_root 时实际落点必须一致
# ============================================================================

def test_custom_workspace_root_relative_write_lands_in_workspace():
    """P0 回归：相对路径写入必须落到 workspace_root 内，而非进程 CWD。"""
    with tempfile.TemporaryDirectory() as root:
        executor = _make_executor_with_root(root)
        cwd_target = os.path.join(os.getcwd(), "relative.txt")
        if os.path.exists(cwd_target):
            os.remove(cwd_target)
        try:
            result = asyncio.run(
                executor.execute_approved(
                    "builtin.files.write_text",
                    {"path": "relative.txt", "content": "hello"},
                    execution_context=ToolExecutionContext(),
                )
            )
            assert not result.is_error, result.output
            assert os.path.exists(os.path.join(root, "relative.txt")), "应落在 workspace_root 内"
            assert not os.path.exists(cwd_target), "相对路径不应落到进程 CWD"
        finally:
            if os.path.exists(cwd_target):
                os.remove(cwd_target)


def test_custom_workspace_root_relative_read_resolves_in_workspace():
    """P0 回归：读取相对路径必须读取 workspace_root 内的文件（证明未读 CWD 同名文件）。"""
    with tempfile.TemporaryDirectory() as root:
        executor = _make_executor_with_root(root)
        with open(os.path.join(root, "note.txt"), "w", encoding="utf-8") as f:
            f.write("ws-content")
        # CWD 内放置同名文件，内容不同，用于证明未读 CWD。
        cwd_note = os.path.join(os.getcwd(), "note.txt")
        had_cwd = os.path.exists(cwd_note)
        if not had_cwd:
            with open(cwd_note, "w", encoding="utf-8") as f:
                f.write("cwd-content")
        try:
            result = asyncio.run(
                executor.execute_approved(
                    "builtin.files.read_text",
                    {"path": "note.txt"},
                    execution_context=ToolExecutionContext(),
                )
            )
            assert not result.is_error, result.output
            assert "ws-content" in result.output
            assert "cwd-content" not in result.output
        finally:
            if not had_cwd and os.path.exists(cwd_note):
                os.remove(cwd_note)


def test_custom_workspace_root_escape_denied_and_not_created():
    """P0 回归：逃逸 workspace_root 的相对路径必须被策略拒绝，且不得在外部创建文件。"""
    with tempfile.TemporaryDirectory() as root:
        executor = _make_executor_with_root(root)
        result = asyncio.run(
            executor.execute_approved(
                "builtin.files.write_text",
                {"path": "../escape.txt", "content": "x"},
                execution_context=ToolExecutionContext(),
            )
        )
        assert result.is_error
        assert result.error_code == "POLICY_DENIED"
        assert not os.path.exists(os.path.join(root, "..", "escape.txt"))


# ============================================================================
# P1：Agent 级策略按 Run 冻结，不保存在全局 Executor
# ============================================================================

def test_agent_policy_isolated_per_run():
    """P1 回归：Agent A 的 deny 规则不得影响 Agent B（delegation 子 Agent 独立）。"""
    def resolver(agent_id: str):
        return {"workspace.read": "deny"} if agent_id == "A" else None

    executor = _make_executor_with_resolver(resolver)
    ctx_a = ToolExecutionContext(agent_id="A")
    ctx_b = ToolExecutionContext(agent_id="B")

    res_a = asyncio.run(
        executor.execute_approved("builtin.files.read_text", {"path": "f.txt"}, execution_context=ctx_a)
    )
    assert res_a.error_code == "POLICY_DENIED", "A 的 deny 规则应生效"

    res_b = asyncio.run(
        executor.execute_approved("builtin.files.read_text", {"path": "f.txt"}, execution_context=ctx_b)
    )
    assert res_b.error_code != "POLICY_DENIED", "B 不应受 A 的规则影响"


def test_run_rules_override_global_baked_agent_rules():
    """P1 回归：Run 提供的 agent 规则必须覆盖全局 baked 的 agent_rules（per-Run 冻结）。"""
    # 全局作用域已 baked workspace.read=deny（模拟主 Agent 规则写入全局的旧行为）。
    scope = default_policy_scope()
    scope.agent_rules = {"workspace.read": PolicyDecision.DENY}
    policy_engine = PolicyEngine(scope)
    executor = ToolExecutor(
        registry=_builtin_registry(),
        approval_manager=ApprovalManager(),
        policy_engine=policy_engine,
        capability_broker=CapabilityBroker(),
        skill_parser=None,
        approval_commands=set(),
        agent_policy_resolver=lambda aid: {"workspace.read": "allow"} if aid == "B" else None,
    )

    # Run B 提供 allow → 应覆盖全局 deny。
    res_b = asyncio.run(
        executor.execute_approved(
            "builtin.files.read_text", {"path": "f.txt"}, execution_context=ToolExecutionContext(agent_id="B")
        )
    )
    assert res_b.error_code != "POLICY_DENIED", "Run 的 allow 应覆盖全局 baked deny"

    # Run 无 agent_id（解析器返回 None）→ 回退全局 baked deny，证明隔离而非全局失效。
    res_none = asyncio.run(
        executor.execute_approved(
            "builtin.files.read_text", {"path": "f.txt"}, execution_context=ToolExecutionContext(agent_id="")
        )
    )
    assert res_none.error_code == "POLICY_DENIED", "无 Run 规则时应回退全局 baked deny"


def test_subagent_without_rules_does_not_inherit_main_agent_deny():
    """四次审计回归：主 Agent 把 workspace.read 收窄为 deny，但 delegation 子 Agent
    无 policy_rules 时，不得继承主 Agent 的 deny，必须按全局 allow 执行——每个 Agent
    只能收窄自身权限，互不继承（修复：factory 不再把主 Agent 规则写入共享 scope，
    _effective_scope 对无规则 Agent 返回独立空 agent_rules 作用域）。
    """
    def resolver(agent_id: str):
        # 主 Agent 收窄为 deny；子 Agent 无规则返回 None。
        return {"workspace.read": "deny"} if agent_id == "main" else None

    # 基础 scope 只保留全局上限（workspace.read=allow），不写入任何 Agent 规则，
    # 模拟修复后的 factory（不再把主 Agent 规则注入共享 scope）。
    scope = default_policy_scope()
    scope.global_rules["workspace.read"] = PolicyDecision.ALLOW
    policy_engine = PolicyEngine(scope)
    executor = ToolExecutor(
        registry=_builtin_registry(),
        approval_manager=ApprovalManager(),
        policy_engine=policy_engine,
        capability_broker=CapabilityBroker(),
        skill_parser=None,
        approval_commands=set(),
        agent_policy_resolver=resolver,
    )

    # 主 Agent 的 deny 生效。
    res_main = asyncio.run(
        executor.execute_approved(
            "builtin.files.read_text", {"path": "f.txt"}, execution_context=ToolExecutionContext(agent_id="main")
        )
    )
    assert res_main.error_code == "POLICY_DENIED", "主 Agent 的 deny 应生效"

    # 子 Agent 无规则 → 继承全局 allow，而非主 Agent 的 deny。
    res_sub = asyncio.run(
        executor.execute_approved(
            "builtin.files.read_text", {"path": "f.txt"}, execution_context=ToolExecutionContext(agent_id="sub")
        )
    )
    assert res_sub.error_code != "POLICY_DENIED", "子 Agent 不应继承主 Agent 的 deny，应按全局 allow 执行"
