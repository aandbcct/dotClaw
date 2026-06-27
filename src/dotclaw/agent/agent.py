"""Agent 角色抽象 —— 持有身份、配置和所有依赖的管理层

Agent 是一个"角色"的抽象：管理自己的会话、LLM 模型、工作区、
工具执行权限、Skill 注册、System Prompt 模板等。

AgentLoop 退化为纯执行引擎，通过 Agent 获取所需的所有依赖。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..llm.base import Message
from .message_utils import trim as msg_trim, clean as msg_clean, _msg_tokens

if TYPE_CHECKING:
    from ..llm.proxy import LLMProxy
    from ..memory.store import Session, SessionManager
    from ..channel.base import Channel
    from ..config import Config
    from ..tools.executor import ToolExecutor
    from ..tools.base import ToolDefinition
    from ..memory.manager import MemoryManager
    from ..skills.registry import SkillRegistry
    from .result import AgentResult
    from ..journal import Journal
    from .slotContext import ContextAssembler, SlotContext
    from .resume import ResumeManager


# ============================================================================
# LLMResponse — 单次 LLM 调用的完整结果
# ============================================================================

@dataclass
class LLMResponse:
    """一次 LLM 调用的完整返回。

    Agent._invoke_llm() 返回此结构，Loop 用它判断下一步：
    有 tool_calls → 执行工具；没有 → 返回最终回复。
    """

    content: str = ""
    """LLM 返回的文本内容"""

    tool_calls: list = field(default_factory=list)
    """LLM 返回的工具调用列表（ToolCall 对象）"""

    finish_reason: str = "stop"
    """停止原因：stop / tool_calls / length / error"""

    input_tokens: int = 0
    """本次调用消耗的输入 token 数"""

    output_tokens: int = 0
    """本次调用产生的输出 token 数"""


def _find_project_root() -> Path:
    """从 dotClaw 模块位置向上找到项目根目录（包含 config.yaml）"""
    import dotclaw
    return Path(dotclaw.__file__).parent.parent.parent


# ============================================================================
# AgentConfig — Agent 级配置（从 daily-assistant.yaml 加载）
# ============================================================================

@dataclass
class AgentConfig:
    """Agent 级配置，从 daily-assistant.yaml 加载。

    与 config.settings.AgentConfig 不同：那个是从 config.yaml 的 agent: 段加载的
    全局 Agent 默认值，这个是单个 Agent 角色的专属配置。
    """

    # ── 身份标识 ──
    agent_id: str = "default"
    """Agent 唯一标识。用于 session 命名空间、日志前缀等"""

    agent_name: str = "DotClaw"
    """Agent 显示名称，注入 system prompt"""

    # ── 模型 ──
    model: str = ""
    """默认模型。"" 表示继承 config.llm.default_model"""

    # ── 工作空间 ──
    workspace: str = "."
    """工作目录。相对路径基于 daily-assistant.yaml 所在目录解析。"" 表示跟随 project_root"""

    # ── 工具过滤 ──
    allowed_tools: list[str] = field(default_factory=list)
    """白名单过滤。空列表 = 所有已注册工具均可用"""

    # ── Skill 过滤 ──
    registered_skills: list[str] = field(default_factory=list)
    """Agent 级 skill 白名单。空 = 且 config.skills.enabled 时使用全局 skill_registry"""

    # ── 循环控制 ──
    max_loop_steps: int = 10
    """ReAct 循环最大迭代次数"""

    # ── System Prompt ──
    system_prompt_template: str = ""
    """Agent 级 system prompt。"" 表示继承 config.agent.system_prompt。
    支持 {agent_name} / {workspace} 占位符替换"""

    # ── 元数据 ──
    description: str = ""
    """Agent 用途描述，用于 human-readable 展示"""

    tags: list[str] = field(default_factory=list)
    """标签，未来用于 Agent 路由"""

    model_params: dict = field(default_factory=dict)
    """模型参数覆盖（temperature / top_p / max_tokens），合并到 LLM 调用参数"""


def load_agent_config(
    agent_id: str = "default",
    path: str | Path | None = None,
) -> AgentConfig:
    """
    加载 {agent_id}.yaml。

    路径解析：
    - 指定 path 时使用指定路径（绝对路径或相对于项目根目录）
    - 未指定时按约定路径：.dotclaw/agentConfig/{agent_id}.yaml
    - 支持 ${ENV_VAR} 环境变量展开
    - YAML 解析失败时返回默认 AgentConfig（agent_id 会写入返回的配置）

    Args:
        agent_id: Agent 标识，用于定位配置文件（默认 "default"）
        path: 显式配置文件路径，可选（优先级高于 agent_id）

    Returns:
        AgentConfig 实例
    """
    if path is not None:
        if Path(path).is_absolute():
            config_path = Path(path)
        else:
            config_path = _find_project_root() / path
    else:
        config_path = _find_project_root() / ".dotclaw" / "agentConfig" / f"{agent_id}.yaml"

    if not config_path.exists():
        return AgentConfig(agent_id=agent_id)

    try:
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception:
        return AgentConfig(agent_id=agent_id)

    # 环境变量展开
    from ..common.utils import expand_env_vars
    raw = expand_env_vars(raw) if raw else {}

    return AgentConfig(
        agent_id=raw.get("agent_id", agent_id),
        agent_name=raw.get("agent_name", "DotClaw"),
        model=raw.get("model", ""),
        workspace=raw.get("workspace", "."),
        allowed_tools=raw.get("allowed_tools", []),
        registered_skills=raw.get("registered_skills", []),
        max_loop_steps=raw.get("max_loop_steps", 10),
        system_prompt_template=raw.get("system_prompt_template", ""),
        description=raw.get("description", ""),
        tags=raw.get("tags", []),
        model_params=raw.get("model_params", {}),
    )


# ============================================================================
# Agent — 角色管理类
# ============================================================================

class Agent:
    """
    Agent 角色抽象 —— 用户面向的对象。

    职责：
    - 持有 AgentConfig + Config + 所有依赖
    - 管理 Session 生命周期（new / switch / list）
    - 构建 SlotContext 数据篮
    - 构建 messages 列表（含 system prompt + _history + 当前消息）
    - 提供 chat() 公开 API（内部创建 AgentLoop 并调用 run）
    - 封装 after-loop 收尾（session 保存 + memory flush）
    """

    def __init__(
        self,
        agent_config: AgentConfig,
        config: "Config",
        llm: "LLMProxy",
        session_mgr: "SessionManager",
        channel: "Channel | None" = None,
        tool_executor: "ToolExecutor | None" = None,
        memory_mgr: "MemoryManager | None" = None,
        skill_registry: "SkillRegistry | None" = None,
        mcp_provider: Any = None,
        memory_dream: Any = None,
        mcp_task: Any = None,
        assembler: "ContextAssembler | None" = None,
        resume_manager: Any = None,
    ):
        """
        通过依赖注入构造 Agent。

        Args:
            agent_config: Agent 级配置（从 daily-assistant.yaml 加载）
            config: 全局 Config（从 config.yaml 加载，用于 model/system_prompt 回退）
            llm: LLM 代理
            session_mgr: 会话管理器
            channel: 通信通道（可为 None，如 Scheduler 场景）
            tool_executor: 工具执行器
            memory_mgr: 记忆管理器
            skill_registry: Skill 注册表
            mcp_provider: MCP 工具提供器（可选）
            memory_dream: DeepDream 记忆蒸馏实例（可选）
            mcp_task: MCP 后台初始化 task（可选）
            assembler: ContextAssembler（可选）
        """
        self.agent_config = agent_config
        self._config = config
        self._llm = llm
        self._session_mgr = session_mgr
        self._session: "Session | None" = None
        self._channel = channel
        self._tool_executor = tool_executor
        self._memory_mgr = memory_mgr
        self._skill_registry = skill_registry
        self._mcp_provider = mcp_provider
        self._memory_dream = memory_dream
        self._mcp_task = mcp_task
        self._assembler: "ContextAssembler | None" = assembler
        self._history: list[Message] = []
        self._resume_manager : ResumeManager = resume_manager

    # ======================== 只读属性 ========================

    @property
    def agent_id(self) -> str:
        """Agent 唯一标识（来自 AgentConfig.agent_id）"""
        return self.agent_config.agent_id

    @property
    def agent_name(self) -> str:
        """Agent 显示名称（来自 AgentConfig.agent_name）"""
        return self.agent_config.agent_name

    @property
    def model(self) -> str:
        """当前模型名。返回 AgentConfig.model 或 config.llm.default_model"""
        return self._resolve_model()

    @model.setter
    def model(self, value: str) -> None:
        """运行时切换模型（写入 AgentConfig.model 覆盖默认值）"""
        self.agent_config.model = value

    @property
    def config(self) -> "Config":
        """全局配置（从 config.yaml 加载）"""
        return self._config

    @property
    def session(self) -> "Session | None":
        """当前活跃会话。None 表示尚未创建/切换"""
        return self._session

    @session.setter
    def session(self, value: "Session | None") -> None:
        """直接设置当前会话（用于 /delete 等场景的回退切换）"""
        self._session = value

    @property
    def llm(self) -> "LLMProxy":
        """LLM 代理"""
        return self._llm

    @property
    def channel(self) -> "Channel | None":
        """通信通道（可为 None，如 Scheduler 场景）"""
        return self._channel

    @property
    def tool_executor(self) -> "ToolExecutor | None":
        """工具执行器"""
        return self._tool_executor

    @property
    def memory_mgr(self) -> "MemoryManager | None":
        """记忆管理器"""
        return self._memory_mgr

    @property
    def skill_registry(self) -> "SkillRegistry | None":
        """Skill 注册表"""
        return self._skill_registry

    @property
    def mcp_provider(self) -> Any:
        """MCP 工具提供器（可为 None）"""
        return self._mcp_provider

    @property
    def memory_dream(self) -> Any:
        """DeepDream 记忆蒸馏实例（可为 None）"""
        return self._memory_dream

    @property
    def assembler(self) -> "ContextAssembler | None":
        """上下文 Assembler（新模式，可为 None 退化到旧 Provider 模式）"""
        return self._assembler

    @property
    def history(self) -> list[Message]:
        """本轮对话历史（tier 3 消息列表，每 turn 追加）"""
        return self._history

    # ======================== 生命周期 ========================

    async def shutdown(self) -> None:
        """关闭 Agent 持有的所有运行时资源（MCP、后台 task 等）。"""
        if self._mcp_task and not self._mcp_task.done():
            self._mcp_task.cancel()
            try:
                await self._mcp_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._mcp_provider:
            await self._mcp_provider.shutdown()

    # ======================== Session 管理 ========================

    async def new_session(self, title: str = "新对话") -> "Session":
        """
        创建新会话并自动切换为当前会话。

        Args:
            title: 会话标题

        Returns:
            新创建的 Session
        """
        self._session = await self._session_mgr.create(
            title=title,
            model=self.model,
        )
        return self._session

    async def switch_session(self, session_id: str) -> "Session | None":
        """
        切换到指定会话。

        Args:
            session_id: 会话 ID

        Returns:
            切换后的 Session，不存在时返回 None
        """
        session = await self._session_mgr.load(session_id)
        if session is not None:
            self._session = session
        return session

    async def list_sessions(self) -> list["Session"]:
        """
        列出所有会话，按更新时间倒序。

        Returns:
            会话列表
        """
        return await self._session_mgr.list_all()

    # ======================== 公开 API ========================

    async def chat(self, message: str) -> "AgentResult":
        """
        处理一条用户消息。

        内部流程：
        1. 确保有活跃 session（无则创建）
        2. 自动检测中断的 request 并恢复 _history
        3. 创建 AgentLoop(self)，调用 loop.run(message)
        4. Loop 内部完成 ReAct 循环后返回 AgentResult

        Args:
            message: 用户输入文本

        Returns:
            AgentResult（final_text / tool_calls_count / iterations / duration_ms）
        """
        if self._session is None:
            await self.new_session()

        # 延迟导入避免循环依赖
        from .loop import AgentLoop

        loop = AgentLoop(self)
        return await loop.run(message)

    #todo 加一个类方法，新建agent

    # ======================== 内部方法（供 AgentLoop 调用） ========================

    def _build_slot_context(self, user_message: str,
                             journal: "Journal") -> "SlotContext":
        """构建 SlotContext（上下文工程的输入参数篮）。

        Args:
            user_message: 用户原始消息
            journal: Journal 观测实例

        Returns:
            SlotContext 数据篮
        """
        from .slotContext import SlotContext as SCtx

        request_id = uuid.uuid4().hex[:8]
        project_root = _find_project_root()
        resolved_system_prompt = self._resolve_system_prompt()
        tool_defs = self._resolve_tool_definitions()

        return SCtx(
            query=user_message,
            request_id=request_id,
            session_id=self._session.id if self._session else "",
            project_root=project_root,
            max_context_tokens=self._config.agent.max_context_tokens,
            system_prompt=resolved_system_prompt,
            tool_definitions=tool_defs,
            skill_registry=self._skill_registry,
            memory_manager=self._memory_mgr,
            knowledge_base=None,
            user_profile=None,
            journal=journal,
        )

    def _build_messages(self, user_input: str,
                         system_prompt: str) -> list[Message]:
        """从 _history + system_prompt 构建 messages。只裁 _history。

        Args:
            user_input: 用户当前输入
            system_prompt: 已组装的 system prompt 文本

        Returns:
            LLM 消息列表
        """
        system_msg = Message(role="system", content=system_prompt)
        user_msg = Message(role="user", content=user_input)

        budget = (self._config.agent.max_context_tokens
                  - _msg_tokens(system_msg)
                  - _msg_tokens(user_msg))

        if budget > 0:
            history = msg_trim(list(self._history), budget)
        else:
            history: list[Message] = []

        return [system_msg] + history + [user_msg]

    async def _invoke_llm(
        self,
        messages: list[Message],
        model: str,
        tool_definitions: list[Any],
        journal: "Journal",
    ) -> LLMResponse:
        """
        调用 LLM 并收集完整响应。

        封装：流式接收 → channel 推送 → 收集文本/tool_calls/token 信息。

        Args:
            messages: 待发送的消息列表
            model: 模型名
            tool_definitions: 工具定义列表
            journal: Journal 观测实例（透传给 llm.chat）

        Returns:
            LLMResponse（content + tool_calls + finish_reason + tokens）
        """
        current_content = ""
        tool_calls: list = []
        finish_reason = "stop"
        input_tokens = 0
        output_tokens = 0

        async for chunk in self._llm.chat(
            messages=messages,
            tools=tool_definitions if tool_definitions else None,
            model=model,
            purpose="chat",
            stream=self._config.llm.stream,
            journal=journal,
        ):
            if chunk.content:
                current_content += chunk.content
                if self._channel:
                    await self._channel.stream(chunk.content)

            if chunk.tool_call:
                tool_calls.append(chunk.tool_call)

            if chunk.is_final:
                finish_reason = chunk.finish_reason or "stop"
                input_tokens = getattr(chunk, "input_tokens", 0)
                output_tokens = getattr(chunk, "output_tokens", len(current_content))
                break

        return LLMResponse(
            content=current_content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def _execute_single_tool(
        self, tc, journal: "Journal | None"
    ) -> Message:
        """
        执行单个工具调用，返回 tool 角色 Message。

        Loop 侧通过 asyncio.gather 并行调用此方法，实现工具并行执行。

        Args:
            tc: ToolCall 对象（有 name / arguments / id 属性）
            journal: Journal 观测实例（resume 场景可为 None）

        Returns:
            role="tool" 的 Message
        """
        import json as _json

        try:
            args = _json.loads(tc.arguments)
        except (_json.JSONDecodeError, TypeError):
            args = {}

        if not self._tool_executor:
            if journal:
                journal.tool_start(tc.name, args=args)
                journal.tool_end(tc.name, result_len=0, status="error", error_type="no_executor")
            return Message(
                role="tool",
                content="错误：工具执行器未初始化",
                tool_call_id=tc.id,
            )

        if self._channel:
            self._channel.print_info(
                f"\n🔧 调用工具: {tc.name}({_json.dumps(args, ensure_ascii=False)})"
            )

        result = await self._tool_executor.execute(
            name=tc.name,
            arguments=args,
            channel=self._channel,
            journal=journal,
        )

        if self._channel:
            self._channel.print_info(
                f"  结果: {result.output[:100]}{'...' if len(result.output) > 100 else ''}"
            )

        return Message(
            role="tool",
            content=result.output,
            tool_call_id=tc.id,
        )

    async def _recall_memory(
        self, query: str, journal: "Journal"
    ) -> str:
        """
        语义检索包装 —— 从 _build_context 中抽出的独立方法。

        Args:
            query: 检索查询文本（通常 = user_message）
            journal: Journal 观测实例

        Returns:
            检索结果文本（memory_summary），无结果时返回 ""
        """
        if not self._memory_mgr:
            return ""

        import logging
        logger = logging.getLogger("dotclaw.agent")

        try:
            journal.memory_retrieval_start()
            results = await self._memory_mgr.search(query, max_results=3)
            journal.memory_retrieval(
                query=query[:100],
                hit_count=len(results),
            )
            if results:
                return "\n".join(
                    f"- ({r.source}:{r.path}) {r.snippet}" for r in results
                )
            return ""
        except Exception as e:
            logger.debug(f"记忆检索失败（不影响对话）: {e}")
            return ""

    async def _flush_memory(
        self, messages: list, journal: "Journal"
    ) -> bool:
        """
        Memory flush 包装 —— 将最近一轮对话写入 L2 记忆。

        Args:
            messages: 最近一轮的 user + assistant 消息（SessionMessage 列表）
            journal: Journal 观测实例

        Returns:
            是否成功写入
        """
        if not self._memory_mgr:
            return False

        try:
            await self._memory_mgr.flush_memory(
                messages=messages,
                reason="round_end",
                journal=journal,
            )
            return True
        except Exception:
            import logging
            logging.getLogger("dotclaw.agent").debug(
                "Memory flush 失败（不影响对话）"
            )
            return False

    async def _finalize_round(
        self, user_message: str, assistant_response: str, journal: "Journal"
    ) -> None:
        """
        After-loop 收尾 —— 由 AgentLoop.run() 在 ReAct 循环结束后调用。

        职责：
        - 将 user + assistant 消息追加到 self.session.messages
        - 调用 self.session_mgr.save(session) 持久化
        - 调用 self._flush_memory() 触发 L2 日记忆写入

        Args:
            user_message: 用户原始消息
            assistant_response: Agent 最终回复
            journal: Journal 观测实例（传递给 flush_memory）
        """
        if self._session is None:
            return

        from ..memory.store import SessionMessage

        self._session.messages.append(SessionMessage(
            role="user",
            content=user_message,
        ))
        self._session.messages.append(SessionMessage(
            role="assistant",
            content=assistant_response,
        ))
        await self._session_mgr.save(self._session)

        # P4：flush 触发
        if self._memory_mgr:
            current_round = self._session.messages[-2:]
            await self._flush_memory(current_round, journal)

    # ======================== 配置解析 ========================

    def _resolve_model(self) -> str:
        """
        解析最终使用的模型名。

        优先级：AgentConfig.model > config.llm.default_model

        Returns:
            模型名
        """
        if self.agent_config.model:
            return self.agent_config.model
        return self._config.llm.default_model

    def _resolve_system_prompt(self) -> str:
        """
        解析最终 system prompt。

        优先级：AgentConfig.system_prompt_template > config.agent.system_prompt
        对 template 进行 {agent_name} / {workspace} 占位符替换。

        Returns:
            最终 system prompt 文本
        """
        template = self.agent_config.system_prompt_template
        if template:
            return template.format(
                agent_name=self.agent_name,
                workspace=self.agent_config.workspace,
            )
        return self._config.agent.system_prompt

    def _resolve_tool_definitions(self) -> list["ToolDefinition"]:
        """
        根据 AgentConfig.allowed_tools 过滤工具定义。

        如果 allowed_tools 为空，返回所有已注册工具。
        如果 tool_executor 未初始化，返回空列表。

        Returns:
            过滤后的工具定义列表
        """
        if not self._tool_executor:
            return []

        all_defs = self._tool_executor.get_definitions()

        allowed = self.agent_config.allowed_tools
        if not allowed:
            return all_defs

        allowed_set = set(allowed)
        return [d for d in all_defs if d.name in allowed_set]
