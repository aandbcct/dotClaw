"""Capability Broker 测试（Tool v1 阶段三）。

覆盖：
- 各 ToolPolicy 档案 → 资源请求种类/档案映射
- 文件路径规范化与 workspace 根目录逃逸检测
- 命令/URL 摘要脱敏（密钥、查询串剥离）
- policy=None 工具的 passthrough（空请求列表）
所有新增注释使用中文。
"""

from __future__ import annotations

import os
import subprocess
import tempfile

import pytest
from pydantic import BaseModel

from dotclaw.tools.capability import (
    CapabilityBroker,
    CapabilityRequest,
    ResourceKind,
    normalize_workspace_path,
)
from dotclaw.tools.decorator import ToolPolicy, get_tool_meta, tool
from dotclaw.tools.function_handler import FunctionToolHandler


class PathArgs(BaseModel):
    path: str = "."


class CmdArgs(BaseModel):
    command: str = "echo hi"


class UrlArgs(BaseModel):
    url: str = "https://example.com"


class ServerArgs(BaseModel):
    server: str = "github"


class NoArgs(BaseModel):
    pass


@tool(name="c.read", description="读文件", policy=ToolPolicy.WORKSPACE_READ, args_model=PathArgs)
async def c_read(args, context):
    return "ok"


@tool(name="c.write", description="写文件", policy=ToolPolicy.WORKSPACE_WRITE, args_model=PathArgs)
async def c_write(args, context):
    return "ok"


@tool(name="c.exec", description="执行命令", policy=ToolPolicy.PROCESS, args_model=CmdArgs)
async def c_exec(args, context):
    return "ok"


@tool(name="c.net", description="网络请求", policy=ToolPolicy.NETWORK, args_model=UrlArgs)
async def c_net(args, context):
    return "ok"


@tool(name="c.mcp", description="MCP 调用", policy=ToolPolicy.MCP, args_model=ServerArgs)
async def c_mcp(args, context):
    return "ok"


@tool(name="c.none", description="无策略工具", args_model=NoArgs)
async def c_none(args, context):
    return "ok"


def _defn(func):
    return FunctionToolHandler(func, get_tool_meta(func)).definition()


def _broker():
    return CapabilityBroker()


def test_read_maps_to_file_read():
    reqs = _broker().resolve(_defn(c_read), PathArgs(path="a.txt"), ".")
    assert len(reqs) == 1
    assert reqs[0].kind is ResourceKind.FILE_READ
    assert reqs[0].profile == "workspace.read"
    assert reqs[0].normalized_path == "a.txt"
    assert reqs[0].escaped is False


def test_write_maps_to_file_write():
    reqs = _broker().resolve(_defn(c_write), PathArgs(path="out.md"), ".")
    assert reqs[0].kind is ResourceKind.FILE_WRITE
    assert reqs[0].profile == "workspace.write"


def test_exec_maps_to_process_with_desensitized_command():
    reqs = _broker().resolve(_defn(c_exec), CmdArgs(command="TOKEN=secret123 echo hi"), ".")
    assert reqs[0].kind is ResourceKind.PROCESS_EXEC
    assert reqs[0].command == "echo hi"          # 环境导出被剥离
    assert "secret123" not in reqs[0].command


def test_net_maps_to_network_http_stripping_query_string():
    reqs = _broker().resolve(_defn(c_net), UrlArgs(url="https://api.x.com/path?token=abc"), ".")
    assert reqs[0].kind is ResourceKind.NETWORK_HTTP
    assert reqs[0].host == "api.x.com"
    assert "token=abc" not in reqs[0].host


def test_mcp_maps_to_mcp_call():
    reqs = _broker().resolve(_defn(c_mcp), ServerArgs(server="github"), ".")
    assert reqs[0].kind is ResourceKind.MCP_CALL
    assert reqs[0].server == "github"


def test_policy_none_is_passthrough():
    reqs = _broker().resolve(_defn(c_none), NoArgs(), ".")
    assert reqs == []


def test_normalize_inside_path_is_relative_and_not_escaped():
    with tempfile.TemporaryDirectory() as root:
        normalized, escaped = normalize_workspace_path(root, "sub/file.txt")
        assert escaped is False
        assert normalized == "sub/file.txt"


def test_normalize_dotdot_escapes_workspace_root():
    with tempfile.TemporaryDirectory() as root:
        normalized, escaped = normalize_workspace_path(root, "../evil.txt")
        assert escaped is True
        # 逃逸时回退为绝对路径（已脱敏，不含内容）
        assert os.path.isabs(normalized.replace("/", os.sep))


def test_normalize_absolute_path_escapes_workspace_root():
    # 计划 §5 显式要求：绝对路径同样视为逃逸（os.path.join 遇绝对路径会覆盖 root）。
    with tempfile.TemporaryDirectory() as root:
        abs_path = os.path.abspath(os.path.join(root, "..", "abs_evil.txt"))
        normalized, escaped = normalize_workspace_path(root, abs_path)
        assert escaped is True
        assert os.path.isabs(normalized.replace("/", os.sep))


def test_real_windows_junction_escape_detected():
    # 完成门槛①：必须在真实 Windows 环境验证符号链接/联接点逃逸。
    # 目录联接点（mklink /J）可由标准用户创建，无需管理员，最接近运行环境。
    if os.name != "nt":
        pytest.skip("仅 Windows 上验证真实联接点逃逸")
    with tempfile.TemporaryDirectory() as root:
        outside = tempfile.mkdtemp(prefix="outside_")
        open(os.path.join(outside, "secret.txt"), "w").close()
        junction = os.path.join(root, "junction")
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", junction, outside],
            capture_output=True,
        )
        if proc.returncode != 0:
            pytest.skip(
                "无法创建联接点（可能缺少权限）："
                + proc.stderr.decode("oem", "replace")
            )
        try:
            # 经联接点访问外部文件：realpath 应穿透重解析点，真实路径落在 workspace 外。
            normalized, escaped = normalize_workspace_path(root, "junction/secret.txt")
            assert escaped is True
            assert os.path.isabs(normalized.replace("/", os.sep))
        finally:
            # 仅移除联接点重解析点，不触碰外部目标目录。
            try:
                os.rmdir(junction)
            except OSError:
                pass


def test_describe_never_leaks_secret_in_command():
    req = CapabilityRequest(
        kind=ResourceKind.PROCESS_EXEC,
        profile="process.exec",
        command=_desensitize("API_KEY=topsecret curl https://x"),
    )
    summary = req.describe()
    assert "topsecret" not in summary
    assert "curl" in summary


def _desensitize(cmd: str) -> str:
    from dotclaw.tools.capability import _desensitize_command

    return _desensitize_command(cmd)
