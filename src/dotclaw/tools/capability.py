"""能力请求与 Broker（Tool v1 阶段三新增）。

Capability Broker 将一次已验证的 Tool Call（工具定义 + 已验证参数）按其声明的
ToolPolicy 档案翻译为结构化的 CapabilityRequest（资源请求）。它不接触用户交互，
也不做最终放行判断——那由 Policy Engine 与 Approval Port 负责。

安全关键点：
- 文件路径执行规范化并检测 workspace 根目录逃逸（..、绝对路径、符号链接/联接点）。
- 命令、URL 等参数在形成摘要时脱敏，绝不把密钥、认证头或原始敏感值带入审计或
  审批提示（总体设计 §4.3 / §7.2）。
- policy=None 的工具不产生任何资源请求，视为 passthrough（由调用方直接放行）。

依赖方向：本模块只依赖 decorator（ToolPolicy）与 base；不依赖 MCP、Channel 或
具体 Runtime 实现。所有新增注释使用中文。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from .base import ToolSource
from .decorator import ToolPolicy


class ResourceKind(str, Enum):
    """资源请求的种类，对应 Broker 能翻译的受保护资源类别。"""

    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    PROCESS_EXEC = "process.exec"
    NETWORK_HTTP = "network.http"
    MCP_CONNECT = "mcp.connect"
    MCP_CALL = "mcp.call"


@dataclass
class CapabilityRequest:
    """一次已验证 Tool Call 所触及资源的结构化描述（总体设计 §3）。

    所有面向审计与审批提示的字段都经过脱敏：命令已剥离环境导出、URL 已去除查询串；
    normalized_path 为相对 workspace 的逻辑路径，absolute_path 仅用于逃逸判定，不写入
    审计展示。
    """

    kind: ResourceKind
    profile: str                       # ToolPolicy 档案值，如 "workspace.write"
    normalized_path: str | None = None  # 文件类：相对 workspace 的逻辑路径
    command: str | None = None          # 进程类：已脱敏的命令
    host: str | None = None             # 网络类：主机名
    service: str | None = None          # 网络类：Provider 服务标识（如 tavily/open_meteo）
    server: str | None = None           # MCP 类：server 名
    escaped: bool = False               # 文件路径是否逃逸 workspace 根目录
    absolute_path: str | None = None    # 文件类：经 workspace_root 解析后的绝对真实路径；
                                         # 供 Executor 回填给 handler，确保实际操作目标与策略
                                         # 检查目标完全一致（P0 修复：自定义 workspace_root 时
                                         # handler 若用 CWD 解析会落到错误位置）。
    param_field: str | None = None      # 文件类：路径参数名（path_param 或默认 "path"），
                                         # 供 Executor 定位需回填的参数。

    def describe(self) -> str:
        """返回脱敏后的资源摘要，供审计与审批提示使用。

        不含任何密钥、认证头或原始敏感值（总体设计 §7.2）。
        """
        if self.kind in (ResourceKind.FILE_READ, ResourceKind.FILE_WRITE):
            verb = "读" if self.kind is ResourceKind.FILE_READ else "写"
            return f"文件{verb}: {self.normalized_path or '(未知)'}"
        if self.kind is ResourceKind.PROCESS_EXEC:
            return f"进程执行: {self.command or '(未知)'}"
        if self.kind is ResourceKind.NETWORK_HTTP:
            svc = f"{self.service}@" if self.service else ""
            return f"网络请求: {svc}{self.host or '(未知)'}"
        if self.kind in (ResourceKind.MCP_CONNECT, ResourceKind.MCP_CALL):
            return f"MCP 调用: {self.server or '(未知)'}"
        return f"资源({self.kind.value})"


# 各 ToolPolicy 档案对应的参数来源字段（从已验证参数中读取）。
# 注意：NETWORK 不再从 Agent 参数读取 url——固定 Provider 的主机由 ToolDefinition
# 的 network_service / network_hosts 静态声明，详见 _network_requests。
_PROFILE_FIELD: dict[ToolPolicy, str] = {
    ToolPolicy.WORKSPACE_READ: "path",
    ToolPolicy.WORKSPACE_WRITE: "path",
    ToolPolicy.PROCESS: "command",
    ToolPolicy.MCP: "server",
}


class CapabilityBroker:
    """将工具定义与已验证参数翻译为 CapabilityRequest 列表。

    每个声明了 ToolPolicy 档案的工具会被翻译成零到一条资源请求；policy=None
    或档案无法识别时返回空列表（passthrough）。
    """

    def resolve(
        self,
        definition: Any,
        validated_args: Any,
        workspace_root: str,
    ) -> list[CapabilityRequest]:
        """根据工具定义与已验证参数形成资源请求列表。

        Args:
            definition: ToolDefinition（含 policy_profile）。
            validated_args: 已校验的 Pydantic 实例或原始字典。
            workspace_root: workspace 根目录，用于文件类路径规范化与逃逸判定。
        """
        profile_value = getattr(definition, "policy_profile", None)

        # MCP 工具：server 是注册期已知元数据，不来自运行参数。
        # 直接形成 mcp.call 资源请求，避免依赖参数中的 server 字段。
        if getattr(definition, "source", None) == ToolSource.MCP:
            server = (definition.metadata or {}).get("server")
            return [self._mcp_request(server or "")]

        if profile_value is None:
            return []
        try:
            policy = ToolPolicy(profile_value)
        except ValueError:
            # 未知档案不翻译，交回调用方按 passthrough 处理。
            return []

        # 网络类：主机来自 ToolDefinition 的静态声明，绝不读取 Agent 参数（§2.2）。
        if policy is ToolPolicy.NETWORK:
            return self._network_requests(definition)

        field = getattr(definition, "path_param", None) or _PROFILE_FIELD.get(policy)
        if field is None:
            return []
        value = _read_field(validated_args, field)
        if value is None:
            # 声明了档案但缺少对应参数：仍形成一条请求，让 Policy 按档案默认判定。
            value = ""

        if policy in (ToolPolicy.WORKSPACE_READ, ToolPolicy.WORKSPACE_WRITE):
            return [self._file_request(policy, str(value), workspace_root, field)]
        if policy is ToolPolicy.PROCESS:
            return [self._process_request(str(value))]
        if policy is ToolPolicy.MCP:
            return [self._mcp_request(str(value))]
        return []

    @staticmethod
    def _file_request(policy: ToolPolicy, path: str, workspace_root: str, field: str) -> CapabilityRequest:
        """形成文件类资源请求，执行路径规范化与逃逸检测。

        field 为路径参数名（path_param 或默认 "path"），absolute_path 为经 workspace_root
        解析后的绝对真实路径，供 Executor 回填给 handler，确保实际操作目标与策略检查目标一致。
        """
        normalized, escaped = normalize_workspace_path(workspace_root, path)
        absolute = resolve_workspace_path(workspace_root, path)
        kind = (
            ResourceKind.FILE_WRITE
            if policy is ToolPolicy.WORKSPACE_WRITE
            else ResourceKind.FILE_READ
        )
        return CapabilityRequest(
            kind=kind,
            profile=policy.value,
            normalized_path=normalized,
            escaped=escaped,
            absolute_path=absolute,
            param_field=field,
        )

    @staticmethod
    def _process_request(command: str) -> CapabilityRequest:
        """形成进程类资源请求，命令已脱敏（剥离环境导出）。"""
        return CapabilityRequest(
            kind=ResourceKind.PROCESS_EXEC,
            profile=ToolPolicy.PROCESS.value,
            command=_desensitize_command(command),
        )

    @staticmethod
    def _network_requests(definition: Any) -> list[CapabilityRequest]:
        """按 ToolDefinition 的静态网络声明生成 NETWORK_HTTP 请求。

        固定 Provider 的主机由 ``network_service`` / ``network_hosts`` 声明，Broker
        不读取任何 Agent 参数（总体设计 §7.2 / 开发计划 §2.2）。一个工具可声明多个
        精确主机（如 Open-Meteo 的地理编码与预报域名），为每个主机生成一条请求。

        安全兜底：声明了 NETWORK 档案却缺少 service 或 hosts 时仍生成一条请求，
        由 Policy Engine 因服务未启用/主机未声明而拒绝（fail-closed，不静默放行）。
        """
        service = getattr(definition, "network_service", None)
        hosts = getattr(definition, "network_hosts", None) or []
        if not service or not hosts:
            return [
                CapabilityRequest(
                    kind=ResourceKind.NETWORK_HTTP,
                    profile=ToolPolicy.NETWORK.value,
                    service=service,
                    host=None,
                )
            ]
        return [
            CapabilityRequest(
                kind=ResourceKind.NETWORK_HTTP,
                profile=ToolPolicy.NETWORK.value,
                service=service,
                host=host,
            )
            for host in hosts
        ]

    @staticmethod
    def _mcp_request(server: str) -> CapabilityRequest:
        """形成 MCP 调用资源请求。"""
        return CapabilityRequest(
            kind=ResourceKind.MCP_CALL,
            profile=ToolPolicy.MCP.value,
            server=server,
        )


def _read_field(validated_args: Any, field: str) -> Any:
    """从已验证参数（Pydantic 实例或字典）中读取指定字段。"""
    if validated_args is None:
        return None
    if isinstance(validated_args, dict):
        return validated_args.get(field)
    return getattr(validated_args, field, None)


def normalize_workspace_path(workspace_root: str, path: str) -> tuple[str, bool]:
    """将相对/绝对路径规范化为相对 workspace 的逻辑路径，并检测根目录逃逸。

    Returns:
        (normalized_relative_path, escaped)
        - normalized_relative_path: 相对 workspace 根的逻辑路径（含 .. 已被解析），
          逃逸时回退为绝对解析路径。
        - escaped: 解析后的真实路径是否落在 workspace 根目录之外（含符号链接/联接点）。

    安全说明（总体设计 §10.2）：先 expanduser 再 realpath，确保与文件/memory
    handler 的 Path(...).expanduser() 行为一致——否则 `~` 会被当作 workspace 内的字面
    目录，而 handler 实际访问真实用户目录，造成逃逸检测被绕过。realpath 进一步解析
    符号链接/联接点，防止通过软链逃逸 workspace。Windows 联接点同样被展开。
    """
    root = os.path.realpath(os.path.expanduser(workspace_root))
    # (root / path) 在遇到绝对 path 时，Path 的 / 运算会让绝对路径覆盖 root，
    # 自然解析到 workspace 之外；resolve() 进一步处理 .. 与冗余分隔符。
    target = os.path.realpath(os.path.join(root, os.path.expanduser(path)))
    real_root = root
    inside = target == real_root or target.startswith(real_root + os.sep)
    if inside:
        rel = os.path.relpath(target, real_root)
        normalized = rel.replace(os.sep, "/")
    else:
        # 逃逸：展示绝对路径（已脱敏，不含内容），便于审计定位。
        normalized = target.replace(os.sep, "/")
    return normalized, not inside


def resolve_workspace_path(workspace_root: str, path: str) -> str:
    """返回 path 相对 workspace_root 解析后的绝对真实路径（不含转义判断）。

    文件/memory handler 必须使用此函数得出的绝对路径作为实际操作目标，确保与
    Broker 的逃逸检查目标完全一致（P0 修复：自定义 workspace_root 时，Broker 检查
    的是 workspace_root 下的路径，handler 若用 CWD 解析就会落到错误位置，安全边界失效）。
    """
    root = os.path.realpath(os.path.expanduser(workspace_root))
    return os.path.realpath(os.path.join(root, os.path.expanduser(path)))


_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def _desensitize_command(command: str) -> str:
    """剥离命令中的环境导出（KEY=VALUE），避免密钥进入审计/审批提示。

    仅去除开头的 `KEY=VALUE` 形式导出；`-c key=value` 这类参数保持不变。
    """
    tokens = command.split()
    kept: list[str] = []
    for token in tokens:
        if _ENV_ASSIGN_RE.match(token) and not token.startswith("-"):
            continue
        kept.append(token)
    return " ".join(kept)


def _desensitize_url(url: str) -> str:
    """去除 URL 查询串（可能含 token），保留协议与主机用于摘要。"""
    return url.split("?", 1)[0]
