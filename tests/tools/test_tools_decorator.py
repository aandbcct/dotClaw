"""tools.decorator 的元数据附着与 ToolDefinition 构建测试（阶段一）。"""

from __future__ import annotations

from pydantic import BaseModel

from dotclaw.tools.base import ToolSource
from dotclaw.tools.decorator import ToolMeta, ToolPolicy, get_tool_meta, tool


class ReadArgs(BaseModel):
    path: str = "默认路径"


def test_tool_decorator_attaches_metadata() -> None:
    """@tool 应把元数据附着到函数，且不执行任何注册动作。"""

    @tool(
        name="builtin.files.read_text",
        args_model=ReadArgs,
        policy=ToolPolicy.WORKSPACE_READ,
        description="读取文件",
    )
    async def read_text(args: ReadArgs) -> str:
        return ""

    meta = get_tool_meta(read_text)
    assert meta is not None
    assert meta.name == "builtin.files.read_text"
    assert meta.args_model is ReadArgs
    assert meta.policy == ToolPolicy.WORKSPACE_READ
    assert meta.description == "读取文件"


def test_tool_meta_build_definition_from_args_model() -> None:
    """build_definition 应由 args_model 生成 Schema 并写入 policy_profile。"""
    meta = ToolMeta(
        name="builtin.memory.read",
        description="读取记忆",
        args_model=ReadArgs,
        policy=ToolPolicy.WORKSPACE_READ,
    )
    definition = meta.build_definition()
    assert definition.name == "builtin.memory.read"
    assert definition.policy_profile == "workspace.read"
    assert definition.source == ToolSource.BUILTIN
    assert definition.parameters["type"] == "object"
    assert "path" in definition.parameters["properties"]


def test_tool_meta_build_definition_without_args_model() -> None:
    """无 args_model 时退化为无参对象，不写入 policy_profile。"""
    meta = ToolMeta(name="builtin.system.get_time", description="时间")
    definition = meta.build_definition()
    assert definition.parameters == {"type": "object", "properties": {}, "required": []}
    assert definition.policy_profile is None
