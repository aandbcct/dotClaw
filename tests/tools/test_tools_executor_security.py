"""ToolExecutor 固定安全链路测试（Tool v1 阶段三）。

固定顺序：校验 → Broker → Policy → 审批 → Handler → Journal（总体设计 §4.5 / §8.1）。
覆盖：
- 入参校验失败不进入 Broker/Policy/Handler（隔离）
- network.http 默认 DENY → POLICY_DENIED（无需 channel）
- process.exec 默认 ASK 且无 Channel → APPROVAL_DENIED
- process.exec ASK 且有 Channel 批准 → 执行；拒绝 → APPROVAL_DENIED
- workspace.read 默认 ALLOW → 执行
- execute_approved 跳过 ASK 执行；但仍受 DENY 阻断（pre_approved 不绕过 deny）
- requires_approval 推导（needs_approval / profile=ask）
- Journal 仅记录脱敏摘要，不含密钥
所有新增注释使用中文。
"""

from __future__ import annotations

import os
import tempfile

from pydantic import BaseModel

from dotclaw.tools.capability import resolve_workspace_path
from dotclaw.tools.decorator import ToolPolicy, get_tool_meta, tool
from dotclaw.tools.executor import ToolExecutor
from dotclaw.tools.function_handler import FunctionToolHandler
from dotclaw.tools.policy import PolicyEngine, PolicyScope, default_policy_scope
from dotclaw.tools.registry import ToolRegistry


class PathArgs(BaseModel):
    path: str = "."


class CmdArgs(BaseModel):
    command: str = "echo hi"


class UrlArgs(BaseModel):
    url: str = "https://example.com"


@tool(name="e.read", description="读文件", policy=ToolPolicy.WORKSPACE_READ, args_model=PathArgs)
async def e_read(args, context):
    return "READ:" + args.path


@tool(name="e.write", description="写文件", policy=ToolPolicy.WORKSPACE_WRITE, needs_approval=True, args_model=PathArgs)
async def e_write(args, context):
    return "WRITE:" + args.path


@tool(name="e.exec", description="执行命令", policy=ToolPolicy.PROCESS, needs_approval=True, args_model=CmdArgs)
async def e_exec(args, context):
    return "EXEC:" + args.command


@tool(name="e.net", description="网络请求", policy=ToolPolicy.NETWORK, args_model=UrlArgs)
async def e_net(args, context):
    return "NET:" + args.url


def _make_registry():
    reg = ToolRegistry()
    for fn in (e_read, e_write, e_exec, e_net):
        reg.register(FunctionToolHandler(fn, get_tool_meta(fn)))
    return reg


def _executor(scope: PolicyScope | None = None) -> ToolExecutor:
    return ToolExecutor(_make_registry(), policy_engine=PolicyEngine(scope) if scope else None)


class FakeChannel:
    def __init__(self, response: str = "y"):
        self.response = response
        self.last_prompt: str | None = None

    async def ask_user(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.response

    async def receive(self) -> str:
        return ""

    async def send(self, message: str) -> None:
        pass

    async def stream(self, chunk: str) -> None:
        pass


class FakeJournal:
    """记录 executor 触发的审计事件（不依赖真实 Journal 会话机制）。"""

    def __init__(self):
        self.tool_start_calls: list[str] = []
        self.tool_end_calls: list[str] = []
        self.policy_calls: list[tuple] = []
        self.approval_calls: list[tuple] = []

    def tool_start(self, name, *a, **k):
        self.tool_start_calls.append(name)

    def tool_end(self, name, **k):
        self.tool_end_calls.append(name)

    def tool_policy_resolved(self, name, decision, matched_rule, capability_summary, **k):
        self.policy_calls.append((name, decision, matched_rule, capability_summary))

    def tool_approval_outcome(self, name, outcome, summary, **k):
        self.approval_calls.append((name, outcome, summary))


async def test_validation_failure_isolated_from_broker_policy():
    journal = FakeJournal()
    res = await _executor().execute("e.read", {"path": 123}, journal=journal)
    assert res.is_error
    assert res.error_code == "INVALID_ARGUMENTS"
    # 校验在 Broker/Policy 之前；策略审计事件不应被触发。
    assert journal.policy_calls == []


async def test_network_default_deny_without_channel():
    res = await _executor().execute("e.net", {"url": "https://x.com"})
    assert res.is_error
    assert res.error_code == "POLICY_DENIED"


async def test_exec_ask_no_channel_denied():
    res = await _executor().execute("e.exec", {"command": "echo hi"})
    assert res.is_error
    assert res.error_code == "APPROVAL_DENIED"


async def test_exec_ask_channel_approved_executes():
    ch = FakeChannel("y")
    res = await _executor().execute("e.exec", {"command": "echo hi"}, channel=ch)
    assert not res.is_error
    assert res.output == "EXEC:echo hi"


async def test_exec_ask_channel_denied():
    ch = FakeChannel("n")
    res = await _executor().execute("e.exec", {"command": "echo hi"}, channel=ch)
    assert res.is_error
    assert res.error_code == "APPROVAL_DENIED"


async def test_read_allow_executes():
    res = await _executor().execute("e.read", {"path": "a.txt"})
    assert not res.is_error
    # P0 修复：handler 收到的是经 workspace_root 解析后的绝对路径（与 Broker 检查目标
    # 一致），而非原始相对路径——否则自定义 workspace_root 时安全边界会失效。
    assert res.output == "READ:" + resolve_workspace_path(".", "a.txt")


async def test_execute_approved_skips_ask():
    # e.write 档案默认 ASK，但 execute_approved 视为已批准。
    res = await _executor().execute_approved("e.write", {"path": "a.txt"})
    assert not res.is_error
    assert res.output == "WRITE:" + resolve_workspace_path(".", "a.txt")


async def test_execute_approved_still_blocked_by_deny():
    # pre_approved 只跳过 ASK，不能绕过 DENY。
    res = await _executor().execute_approved("e.net", {"url": "https://x.com"})
    assert res.is_error
    assert res.error_code == "POLICY_DENIED"


async def test_requires_approval_derivation():
    ex = _executor()
    assert ex.requires_approval("e.write") is True     # 声明式 needs_approval
    assert ex.requires_approval("e.exec") is True      # 档案默认 ASK
    assert ex.requires_approval("e.read") is False      # 档案 ALLOW
    assert ex.requires_approval("e.net") is False       # 档案 DENY（被拒，不达审批）
    assert ex.requires_approval("not_a_tool") is False


async def test_journal_records_desensitized_summary_only():
    ch = FakeChannel("y")
    journal = FakeJournal()
    # 命令含密钥；断言写入审计的摘要不含密钥。
    await _executor().execute("e.exec", {"command": "TOKEN=topsecret echo hi"}, channel=ch, journal=journal)
    for _, _, _, summary in journal.policy_calls:
        assert "topsecret" not in summary
        assert "echo hi" in summary
    for _, outcome, summary in journal.approval_calls:
        assert outcome == "approved"
        assert "topsecret" not in summary


async def test_workspace_escape_blocked_by_policy():
    with tempfile.TemporaryDirectory() as root:
        scope = PolicyScope(
            global_rules=dict(default_policy_scope().global_rules),
            workspace_root=root,
        )
        res = await _executor(scope).execute("e.read", {"path": "../evil.txt"})
        assert res.is_error
        assert res.error_code == "POLICY_DENIED"
