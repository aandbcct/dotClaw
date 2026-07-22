"""AgentIdentity — Agent 角色声明式约束。

纯 dataclass，零依赖。只定义"Agent 被允许做什么"，不持有任何可执行对象。
Agent 方法中用 Identity 约束 Runtime：allowed_tools 过滤 tool_executor，
model 选择 LLM 调用，system_prompt_template 生成行为指令。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class AgentIdentity:
    """Agent 角色声明式约束 —— 纯数据，零运行时依赖。

    Identity 定义三个维度的约束：
    1. 身份：id + name（注入 system prompt 占位符）
    2. 权限：allowed_tools（白名单过滤，空=全部允许）
    3. 行为：system_prompt_template + model + max_loop_steps
    """

    # ── 身份标识 ──
    agent_id: str
    """Agent 唯一标识。用于配置文件定位、session 命名空间等。"""

    agent_name: str = ""
    """Agent 显示名称，会注入 system prompt ({agent_name} 占位符)。"""

    # ── 权限约束 ──
    allowed_tools: list[str] = field(default_factory=list)
    """工具白名单。空列表 = 所有已注册工具均可用。"""

    # ── 策略约束（Agent 级只能收窄全局上限）──
    policy_rules: dict[str, str] | None = None
    """Agent 级策略规则（profile -> allow/ask/deny）。仅用于收窄全局上限，
    不能放宽；具体收窄判定由 PolicyEngine 在评估时执行。None = 不附加 Agent 规则。"""

    # ── 行为约束 ──
    system_prompt_template: str = ""
    """Agent 级 system prompt 模板。
    支持 {agent_name} / {workspace} 占位符。
    "" 表示回退到 config.agent.system_prompt。"""

    model: str = ""
    """默认模型。"" 表示回退到 config.llm.default_model。"""

    max_loop_steps: int = 10
    """ReAct 循环最大迭代次数。"""

    workspace: str = ""
    """工作目录。用于 system prompt 中 {workspace} 占位符替换。"""

    # ── 元数据 ──
    description: str = ""
    """Agent 用途描述，用于 human-readable 展示。"""

    tags: list[str] = field(default_factory=list)
    """标签，未来用于 Agent 路由。"""

    # ── A2A AgentCard ──
    capabilities: list[str] = field(default_factory=list)
    """能力标签。对标 A2A AgentCard.skills。
    示例：["web_search", "code_generation", "file_ops", "memory_retrieval"]。
    用于父 Agent 决定 spawn 哪个子 Agent。"""

    input_modes: list[str] = field(default_factory=lambda: ["text"])
    """支持的输入模式。对标 A2A AgentCard.defaultInputModes。
    示例：["text", "file", "structured_data"]。"""

    output_modes: list[str] = field(default_factory=lambda: ["text"])
    """支持的输出模式。对标 A2A AgentCard.defaultOutputModes。
    示例：["text", "file", "structured_data"]。"""

    context_slot_ids: tuple[str, ...] | None = None
    """Agent Owner 显式启用的 Context Slot；None 时使用 Context 组合根的默认计划。"""

    # ── 方法 ──

    def resolve_system_prompt(self) -> str:
        """用 Identity 字段替换 system_prompt_template 中的占位符。

        占位符：{agent_name} / {workspace}

        Returns:
            替换后的 system prompt 文本。template 为空时返回 ""（由调用方回退到 config）。
        """
        template = self.system_prompt_template
        if not template:
            return ""
        return template.format(
            agent_name=self.agent_name,
            workspace=self.workspace,
        )

    def resolve_model(self, default_model: str) -> str:
        """解析最终使用的模型名。

        Identity.model 为空时回退到传入的 default_model。

        Args:
            default_model: 全局默认模型名（通常来自 config.llm.default_model）

        Returns:
            最终模型名
        """
        return self.model or default_model


# ============================================================================
# load_agent_config — 从 YAML 加载 AgentIdentity
# ============================================================================

def _find_project_root() -> Path:
    """从 dotClaw 模块位置向上找到项目根目录（包含 config.yaml）。"""
    import dotclaw
    return Path(dotclaw.__file__).parent.parent.parent


def load_agent_config(
    agent_id: str = "default",
    path: str | Path | None = None,
) -> AgentIdentity:
    """从 {agent_id}.yaml 加载 AgentIdentity。

    路径解析：
    - 指定 path 时使用指定路径（绝对路径或相对于项目根目录）
    - 未指定时按约定路径：.dotclaw/agentConfig/{agent_id}.yaml
    - 支持 ${ENV_VAR} 环境变量展开
    - YAML 解析失败时返回默认 AgentIdentity（agent_id 会写入返回的配置）

    Args:
        agent_id: Agent 标识，用于定位配置文件（默认 "default"）
        path: 显式配置文件路径，可选（优先级高于 agent_id）

    Returns:
        AgentIdentity 实例
    """
    if path is not None:
        if Path(path).is_absolute():
            config_path = Path(path)
        else:
            config_path = _find_project_root() / path
    else:
        config_path = _find_project_root() / ".dotclaw" / "agentConfig" / f"{agent_id}.yaml"

    if not config_path.exists():
        return AgentIdentity(agent_id=agent_id)

    try:
        with open(config_path, encoding="utf-8") as f:
            raw: dict = yaml.safe_load(f) or {}
    except Exception:
        return AgentIdentity(agent_id=agent_id)

    # 环境变量展开
    from ..common.utils import expand_env_vars
    raw = expand_env_vars(raw) if raw else {}

    raw_context_slot_ids = raw.get("context_slot_ids")
    context_slot_ids: tuple[str, ...] | None = (
        tuple(item for item in raw_context_slot_ids if isinstance(item, str))
        if isinstance(raw_context_slot_ids, list) and all(isinstance(item, str) for item in raw_context_slot_ids)
        else None
    )
    raw_policy_rules = raw.get("policy_rules")
    policy_rules: dict[str, str] | None = None
    if isinstance(raw_policy_rules, dict):
        # 仅保留 profile -> 决策字符串 的合法条目；非法值忽略，由 PolicyEngine 兜底。
        policy_rules = {
            str(k): str(v)
            for k, v in raw_policy_rules.items()
            if isinstance(k, str) and str(v) in ("allow", "ask", "deny")
        } or None
    return AgentIdentity(
        agent_id=raw.get("agent_id", agent_id),
        agent_name=str(raw.get("agent_name", "DotClaw")),
        model=str(raw.get("model", "")),
        workspace=str(raw.get("workspace", ".")),
        allowed_tools=list(raw.get("allowed_tools", [])),
        max_loop_steps=int(raw.get("max_loop_steps", 10)),
        system_prompt_template=str(raw.get("system_prompt_template", "")),
        description=str(raw.get("description", "")),
        tags=list(raw.get("tags", [])),
        capabilities=list(raw.get("capabilities", [])),
        input_modes=list(raw.get("input_modes", ["text"])),
        output_modes=list(raw.get("output_modes", ["text"])),
        context_slot_ids=context_slot_ids,
        policy_rules=policy_rules,
    )
