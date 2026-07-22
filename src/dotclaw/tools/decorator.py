"""工具装饰器与策略档案（Tool v1 阶段一新增）。

@tool 只负责把声明元数据附着到函数上，不在导入时向全局 Registry 写入状态。
真正的注册由阶段二的 ToolDiscovery 完成。所有新增注释使用中文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

from .base import ToolDefinition, ToolExecutionContext, ToolSource


class ToolPolicy(str, Enum):
    """内置资源访问档案（总体设计 §4.3）。

    工具作者只能从中选择，不能自由组合能力；Broker 按档案与参数自动形成
    资源请求。MCP 的 MCP(server) 档案由 Provider 自动生成，不在此枚举。
    """

    WORKSPACE_READ = "workspace.read"
    WORKSPACE_WRITE = "workspace.write"
    PROCESS = "process.exec"
    NETWORK = "network.http"
    MCP = "mcp.call"


@dataclass(frozen=True)
class ToolMeta:
    """@tool 装饰器附着的元数据对象。

    仅描述工具自身，不持有全局状态。FunctionToolHandler 依据它构造
    ToolDefinition 并调用被装饰函数。

    args_style 决定已验证参数如何传入被装饰函数：
    - "model"：函数第一个非上下文形参接收整个 args_model 实例（显式模型场景）。
    - "fields"：函数各业务形参直接对应 args_model 的字段，由 Discovery 从签名
      推导后拆包传入（签名推导场景）。
    """

    name: str
    description: str
    args_model: type[BaseModel] | None = None
    policy: ToolPolicy | None = None
    source: ToolSource = ToolSource.BUILTIN
    needs_approval: bool = False
    timeout: float = 60.0
    metadata: dict = field(default_factory=dict)
    args_style: str = "model"
    path_param: str | None = None

    def build_definition(self) -> ToolDefinition:
        """由元数据构造面向 LLM 的 ToolDefinition。

        parameters 来自 args_model 的 JSON Schema；无 args_model 时退化为
        无参对象。
        """
        from .schema import to_json_schema

        if self.args_model is not None:
            parameters: dict = to_json_schema(self.args_model)
        else:
            parameters = {"type": "object", "properties": {}, "required": []}
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=parameters,
            source=self.source,
            needs_approval=self.needs_approval,
            timeout=self.timeout,
            metadata=dict(self.metadata),
            policy_profile=self.policy.value if self.policy is not None else None,
            path_param=self.path_param,
        )


FuncT = TypeVar("FuncT", bound=Callable[..., Any])


def tool(
    *,
    name: str,
    description: str,
    args_model: type[BaseModel] | None = None,
    policy: ToolPolicy | None = None,
    source: ToolSource = ToolSource.BUILTIN,
    needs_approval: bool = False,
    timeout: float = 60.0,
    metadata: dict | None = None,
    path_param: str | None = None,
) -> Callable[[FuncT], FuncT]:
    """声明一个工具的元数据。

    用法：
        @tool(name="builtin.files.read_text", args_model=ReadTextArgs,
              policy=ToolPolicy.WORKSPACE_READ)
        async def read_text(args: ReadTextArgs, context: ToolExecutionContext) -> str:
            ...

    该装饰器仅把 ToolMeta 记录到 func.__tool_meta__，不执行任何注册动作，
    也不导入全局 Registry。
    """

    def _wrap(func: FuncT) -> FuncT:
        meta = ToolMeta(
            name=name,
            description=description,
            args_model=args_model,
            policy=policy,
            source=source,
            needs_approval=needs_approval,
            timeout=timeout,
            metadata=metadata or {},
            path_param=path_param,
        )
        func.__tool_meta__ = meta  # type: ignore[attr-defined]
        return func

    return _wrap


def get_tool_meta(func: Callable[..., Any]) -> ToolMeta | None:
    """从函数上读取 @tool 元数据（若存在）。"""
    return getattr(func, "__tool_meta__", None)
