"""8 个 builtin 工具迁移验证（Tool v1 阶段二）。

逐个验证：新规范名、JSON Schema（必填/默认值）、以及现有业务行为不变。
所有新增注释使用中文。
"""

from __future__ import annotations

import pytest

from dotclaw.tools.base import ToolExecutionContext
from dotclaw.tools.decorator import ToolPolicy
from dotclaw.tools.discovery import ToolDiscovery

EXPECTED_NAMES = {
    "builtin.files.read_text",
    "builtin.files.write_text",
    "builtin.files.list_directory",
    "builtin.process.execute",
    "builtin.memory.read",
    "builtin.memory.write",
    "builtin.system.get_info",
    "builtin.system.get_time",
}


@pytest.fixture()
def handlers():
    return {h.name: h for h in ToolDiscovery.discover_builtin()}


def test_all_eight_builtins_discovered(handlers) -> None:
    assert set(handlers.keys()) == EXPECTED_NAMES


def test_policies_assigned(handlers) -> None:
    assert handlers["builtin.files.read_text"].definition().policy_profile == ToolPolicy.WORKSPACE_READ.value
    assert handlers["builtin.files.write_text"].definition().policy_profile == ToolPolicy.WORKSPACE_WRITE.value
    assert handlers["builtin.files.list_directory"].definition().policy_profile == ToolPolicy.WORKSPACE_READ.value
    assert handlers["builtin.process.execute"].definition().policy_profile == ToolPolicy.PROCESS.value
    assert handlers["builtin.memory.read"].definition().policy_profile == ToolPolicy.WORKSPACE_READ.value
    assert handlers["builtin.memory.write"].definition().policy_profile == ToolPolicy.WORKSPACE_WRITE.value
    # 系统类工具无受保护资源，policy 留空。
    assert handlers["builtin.system.get_info"].definition().policy_profile is None
    assert handlers["builtin.system.get_time"].definition().policy_profile is None


def test_approval_flags(handlers) -> None:
    assert handlers["builtin.files.write_text"].definition().needs_approval is True
    assert handlers["builtin.process.execute"].definition().needs_approval is True
    assert handlers["builtin.memory.write"].definition().needs_approval is True
    assert handlers["builtin.files.read_text"].definition().needs_approval is False


def test_schemas_required_and_defaults(handlers) -> None:
    rt = handlers["builtin.files.read_text"].definition()
    assert rt.parameters["required"] == ["path"]

    wt = handlers["builtin.files.write_text"].definition()
    assert set(wt.parameters["required"]) == {"path", "content"}

    ld = handlers["builtin.files.list_directory"].definition()
    assert ld.parameters["properties"]["path"]["default"] == "."

    ex = handlers["builtin.process.execute"].definition()
    assert ex.parameters["required"] == ["command"]

    mr = handlers["builtin.memory.read"].definition()
    assert mr.parameters["properties"]["long_term_file"]["default"] == "./data/memory/MEMORY.md"

    mw = handlers["builtin.memory.write"].definition()
    assert mw.parameters["required"] == ["content"]

    # 无参工具：空 required。
    assert handlers["builtin.system.get_info"].definition().parameters["required"] == []
    assert handlers["builtin.system.get_time"].definition().parameters["required"] == []


async def test_read_write_text(handlers, tmp_path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")

    read = await handlers["builtin.files.read_text"].execute(
        {"path": str(f)}, ToolExecutionContext()
    )
    assert read.output == "hello"

    out = tmp_path / "b.txt"
    write = await handlers["builtin.files.write_text"].execute(
        {"path": str(out), "content": "world"}, ToolExecutionContext()
    )
    assert "成功写入" in write.output
    assert out.read_text(encoding="utf-8") == "world"


async def test_list_directory(handlers, tmp_path) -> None:
    (tmp_path / "x.txt").write_text("1")
    (tmp_path / "sub").mkdir()
    res = await handlers["builtin.files.list_directory"].execute(
        {"path": str(tmp_path)}, ToolExecutionContext()
    )
    assert "x.txt" in res.output
    assert "sub/" in res.output


async def test_execute_runs_command(handlers) -> None:
    res = await handlers["builtin.process.execute"].execute(
        {"command": "echo hello"}, ToolExecutionContext()
    )
    assert "hello" in res.output


async def test_memory_read_write(handlers, tmp_path) -> None:
    mem = tmp_path / "MEMORY.md"
    w = await handlers["builtin.memory.write"].execute(
        {"content": "note", "long_term_file": str(mem)}, ToolExecutionContext()
    )
    assert "已追加" in w.output
    r = await handlers["builtin.memory.read"].execute(
        {"long_term_file": str(mem)}, ToolExecutionContext()
    )
    assert "note" in r.output


async def test_system_tools(handlers) -> None:
    info = await handlers["builtin.system.get_info"].execute(
        {}, ToolExecutionContext()
    )
    assert "操作系统" in info.output

    now = await handlers["builtin.system.get_time"].execute(
        {}, ToolExecutionContext()
    )
    # 时间格式 YYYY-MM-DD HH:MM:SS
    assert len(now.output) == 19
    assert now.output[4] == "-"
