"""Approval Port 测试（Tool v1 阶段三重构）。

核心不变量（总体设计 §4.3）：无交互通道（Channel）时 ask 必须拒绝，不能默认放行。
覆盖：
- 无 Channel → 拒绝（False）
- 有 Channel 且用户确认 → 批准（True）
- 有 Channel 且用户拒绝 → 拒绝（False）
- 端口停用（set_enabled(False)）→ 拒绝
- 展示给用户的脱敏摘要不得含密钥
所有新增注释使用中文。
"""

from __future__ import annotations

from dotclaw.tools.approval import ApprovalManager
from dotclaw.tools.capability import CapabilityRequest, ResourceKind


class FakeChannel:
    """最小 Channel 桩：记录询问提示，按预设响应回答。"""

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


async def test_no_channel_denies():
    approved = await ApprovalManager().request("文件写: a.txt")
    assert approved is False


async def test_channel_yes_approves():
    ch = FakeChannel("y")
    approved = await ApprovalManager().request("文件写: a.txt", ch)
    assert approved is True
    assert "文件写: a.txt" in ch.last_prompt


async def test_channel_no_denies():
    ch = FakeChannel("n")
    approved = await ApprovalManager().request("文件写: a.txt", ch)
    assert approved is False


async def test_disabled_port_denies_even_with_channel():
    mgr = ApprovalManager()
    mgr.set_enabled(False)
    ch = FakeChannel("y")
    assert await mgr.request("文件写: a.txt", ch) is False


async def test_summary_presented_to_user_contains_no_secret():
    # 脱敏发生在 Broker 阶段：经 Broker 生成的请求已剥离环境导出（密钥）。
    from dotclaw.tools.capability import CapabilityBroker
    from dotclaw.tools.decorator import ToolPolicy, get_tool_meta, tool
    from dotclaw.tools.function_handler import FunctionToolHandler
    from pydantic import BaseModel

    class CmdArgs(BaseModel):
        command: str = "echo hi"

    @tool(name="a.exec", description="执行命令", policy=ToolPolicy.PROCESS, args_model=CmdArgs)
    async def a_exec(args, context):
        return "ok"

    defn = FunctionToolHandler(a_exec, get_tool_meta(a_exec)).definition()
    req = CapabilityBroker().resolve(defn, CmdArgs(command="TOKEN=supersecret echo hi"), ".")[0]
    summary = req.describe()  # 进程执行: echo hi（无密钥）
    ch = FakeChannel("y")
    await ApprovalManager().request(summary, ch)
    assert "supersecret" not in ch.last_prompt
    assert "echo hi" in ch.last_prompt
