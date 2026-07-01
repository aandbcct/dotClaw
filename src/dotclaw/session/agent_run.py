"""AgentRun —— 一次原子调用的完整记录。

AgentRun 是 dotClaw 的最小执行单元：一次连续的 LLM 上下文窗口占用周期。
当需要释放上下文、等待外部输入或切换执行 Agent 时，当前 AgentRun 结束，开启新 AgentRun。

AgentRun 持久化到 agent_runs/{run_id}.json，支持中断恢复。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..llm.base import Message


@dataclass
class AgentRun:
    """一次原子调用的完整记录。

    字段：
        run_id: 本次原子调用的唯一标识
        agent_id: 执行本次调用的 Agent ID
        parent_run_id: 父 AgentRun ID（子 Agent 场景，无则为 ""）
        messages: LLM 返回内容 + 工具调用内容的完整记录
        end_status: 结束状态（completed / handoff / failed）
        tool_calls: 本次调用的工具调用次数
        tokens_in: 输入 token 数
        tokens_out: 输出 token 数
        iterations: ReAct 循环迭代次数
        duration_ms: 执行耗时（毫秒）
        error: 异常信息（仅在 failed 时非空）
        started_at: 开始时间
        ended_at: 结束时间
    """

    run_id: str
    """本次原子调用的唯一标识"""

    agent_id: str = ""
    """执行本次调用的 Agent ID"""

    parent_run_id: str = ""
    """父 AgentRun ID。子 Agent 场景非空，根 AgentRun 为空字符串。"""

    messages: list[Message] = field(default_factory=list)
    """本次原子调用中 LLM 返回内容 + 工具调用内容的完整记录"""

    end_status: str = "completed"
    """结束状态：completed（正常完成）、handoff（切换 Agent）、failed（异常）"""

    tool_calls: int = 0
    """本次调用的工具调用次数"""

    tokens_in: int = 0
    """输入 token 数"""

    tokens_out: int = 0
    """输出 token 数"""

    iterations: int = 0
    """ReAct 循环迭代次数"""

    duration_ms: int = 0
    """执行耗时（毫秒）"""

    error: str | None = None
    """异常信息（仅在 end_status="failed" 时非空）"""

    started_at: str = ""
    """开始时间（ISO 8601）"""

    ended_at: str = ""
    """结束时间（ISO 8601）"""

    # ── 序列化 ──

    def to_dict(self) -> dict:
        """序列化为 dict。"""
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "parent_run_id": self.parent_run_id,
            "messages": [
                {"role": m.role, "content": m.content,
                 "tool_calls": m.tool_calls, "tool_call_id": m.tool_call_id,
                 "name": m.name}
                for m in self.messages
            ],
            "end_status": self.end_status,
            "tool_calls": self.tool_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "iterations": self.iterations,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AgentRun:
        """从 dict 反序列化。"""
        msgs_data: list[dict] = data.pop("messages", [])
        run: AgentRun = cls(**{k: v for k, v in data.items() if k != "messages"})
        run.messages = [
            Message(
                role=m.get("role", ""),
                content=m.get("content", ""),
                tool_calls=m.get("tool_calls"),
                tool_call_id=m.get("tool_call_id"),
                name=m.get("name"),
            )
            for m in msgs_data
        ]
        return run

    @property
    def final_output(self) -> str | None:
        """提取最终输出文本。

        从 messages 中找最后一条 role="assistant" 且无 tool_calls 的消息。
        如果不存在这样的消息（如 handoff），返回 None。
        """
        for m in reversed(self.messages):
            if m.role == "assistant" and not m.tool_calls:
                return m.content
        return None


# ============================================================================
# AgentRunManager
# ============================================================================

class AgentRunManager:
    """AgentRun 持久化管理器。

    每个 AgentRun 存储为独立 JSON 文件：{data_dir}/agent_runs/{run_id}.json
    """

    def __init__(self, data_dir: str | Path) -> None:
        """初始化。

        Args:
            data_dir: 数据目录路径
        """
        import dotclaw
        module_path: Path = Path(dotclaw.__file__).parent
        project_root: Path = module_path.parent.parent
        self._data_dir: Path = project_root / data_dir / "agent_runs"
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def _run_path(self, run_id: str) -> Path:
        """获取 AgentRun 文件路径。"""
        return self._data_dir / f"{run_id}.json"

    async def save(self, run: AgentRun) -> None:
        """保存 AgentRun 到磁盘。"""
        import aiofiles
        path: Path = self._run_path(run.run_id)
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(
                run.to_dict(), ensure_ascii=False, indent=2
            ))

    async def load(self, run_id: str) -> AgentRun | None:
        """加载 AgentRun。返回 None 如果不存在。"""
        import aiofiles
        path: Path = self._run_path(run_id)
        if not path.exists():
            return None
        try:
            async with aiofiles.open(path, encoding="utf-8") as f:
                data: str = await f.read()
            return AgentRun.from_dict(json.loads(data))
        except Exception:
            return None
