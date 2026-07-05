"""AgentRun —— 一次原子调用的完整记录。

AgentRun 是 dotClaw 的最小执行单元：一次 LLM 推理-执行的原子步。
每次 LLM 调用产生一个新的 AgentRun，工具调用后 TurnLoop 创建新 AgentRun 继续。

持久化位置：session/{session_id}/agent_runs/{run_id}.json

流转消息不再存储在 AgentRun 中，而是通过 Journal 以 Trace Event 形式写入 trace.jsonl。
AgentRun 仅持有 state_snapshot + trace_ids + 统计元数据。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


# ============================================================================
# 枚举定义
# ============================================================================

class RunEndStatus(Enum):
    """AgentRun 的结束状态。

    AgentRun 结束后不可恢复，新的事件触发会创建新的 AgentRun。
    """

    COMPLETED = "completed"
    """正常完成：LLM 返回了文本回复"""

    HANDOFF = "handoff"
    """任务流转：控制权转交其他 Agent"""

    FAILED = "failed"
    """执行异常：LLM 调用或工具执行失败"""

    INTERRUPTED = "interrupted"
    """被中断：用户主动中断或进程崩溃"""

    TOOL_WAIT = "tool_wait"
    """工具等待：已发出工具调用，等待异步结果返回"""


class TriggerType(Enum):
    """AgentRun 的触发源类型。

    任何导致当前执行流中断并需要重新唤醒的事件都是触发源。
    """

    USER_INPUT = "user_input"
    """用户发来新消息"""

    TOOL_RESULT = "tool_result"
    """外部工具执行完毕返回的结果"""

    RESUME = "resume"
    """从挂起/崩溃状态恢复"""

    TIMER = "timer"
    """定时器触发（预留）"""

    APPROVAL_DONE = "approval_done"
    """人工审批完成（预留）"""


# ============================================================================
# AgentRun —— 执行记录
# ============================================================================

@dataclass
class AgentRun:
    """一次原子 LLM 调用的完整记录。

    字段：
        run_id: 本次 AgentRun 唯一标识
        agent_id: 执行本次调用的 Agent ID
        parent_run_id: 父 AgentRun ID（子 Agent 场景，无则为 ""）
        end_status: 结束状态（RunEndStatus 枚举）
        state_snapshot: AgentRun 结束时的状态快照（dict）
        trace_ids: 关联的 Trace Event IDs（list[str]）
        trigger: 触发源类型（TriggerType.value）
        sequence: 在 Session 中的全局序号
        tool_calls: 本次 LLM 响应中的工具调用数
        tokens_in: 输入 token 数
        tokens_out: 输出 token 数
        duration_ms: 执行耗时（毫秒）
        error: 异常信息（仅在 FAILED / INTERRUPTED 时非空）
        started_at: 开始时间（ISO 8601）
        ended_at: 结束时间（ISO 8601）
    """

    run_id: str
    """本次 AgentRun 唯一标识"""

    agent_id: str = ""
    """执行本次调用的 Agent ID"""

    parent_run_id: str = ""
    """父 AgentRun ID。子 Agent 场景非空，根 AgentRun 为空字符串。"""

    end_status: str = RunEndStatus.COMPLETED.value
    """结束状态：completed / handoff / failed / interrupted / tool_wait"""

    state_snapshot: dict | None = None
    """AgentRun 结束时的 AgentState 快照。用于恢复执行上下文。"""

    trace_ids: list[str] = field(default_factory=list)
    """关联的 Trace Event IDs。指向 trace.jsonl 中的具体事件行。"""

    trigger: str = TriggerType.USER_INPUT.value
    """触发源类型：user_input / tool_result / resume / timer / approval_done"""

    sequence: int = 0
    """在所属 Session 中的全局序号（从 0 开始递增）"""

    tool_calls: int = 0
    """本次 LLM 响应中的工具调用数（通常 0 或 1 次调用含多个 tool）"""

    tokens_in: int = 0
    """输入 token 数"""

    tokens_out: int = 0
    """输出 token 数"""

    duration_ms: int = 0
    """执行耗时（毫秒）"""

    error: str | None = None
    """异常信息（仅在 end_status=failed 或 interrupted 时非空）"""

    started_at: str = ""
    """开始时间（ISO 8601）"""

    ended_at: str = ""
    """结束时间（ISO 8601）"""

    # ── 序列化 ──

    def to_dict(self) -> dict:
        """序列化为 dict。不再包含 messages 字段。"""
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "parent_run_id": self.parent_run_id,
            "end_status": self.end_status,
            "state_snapshot": self.state_snapshot,
            "trace_ids": self.trace_ids,
            "trigger": self.trigger,
            "sequence": self.sequence,
            "tool_calls": self.tool_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AgentRun:
        """从 dict 反序列化。"""
        return cls(
            run_id=data.get("run_id", ""),
            agent_id=data.get("agent_id", ""),
            parent_run_id=data.get("parent_run_id", ""),
            end_status=data.get("end_status", RunEndStatus.COMPLETED.value),
            state_snapshot=data.get("state_snapshot"),
            trace_ids=data.get("trace_ids", []),
            trigger=data.get("trigger", TriggerType.USER_INPUT.value),
            sequence=data.get("sequence", 0),
            tool_calls=data.get("tool_calls", 0),
            tokens_in=data.get("tokens_in", 0),
            tokens_out=data.get("tokens_out", 0),
            duration_ms=data.get("duration_ms", 0),
            error=data.get("error"),
            started_at=data.get("started_at", ""),
            ended_at=data.get("ended_at", ""),
        )

    @property
    def final_output(self) -> str | None:
        """提取最终输出文本。

        不再从 messages 中查找——改为从 state_snapshot 提取。
        如果 state_snapshot 不存在，返回 None。
        """
        if self.state_snapshot is None:
            return None
        return self.state_snapshot.get("final_output")


# ============================================================================
# AgentRunManager
# ============================================================================

class AgentRunManager:
    """AgentRun 持久化管理器。

    每个 AgentRun 存储为独立 JSON 文件：{data_dir}/session/{session_id}/agent_runs/{run_id}.json
    """

    def __init__(self, data_dir: str | Path) -> None:
        """初始化。

        Args:
            data_dir: 数据目录路径（Session 基础目录）
        """
        import dotclaw
        module_path: Path = Path(dotclaw.__file__).parent
        project_root: Path = module_path.parent.parent
        self._base_dir: Path = (project_root / data_dir).resolve()
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _run_path(self, session_id: str, run_id: str) -> Path:
        """获取 AgentRun 文件路径。

        Args:
            session_id: Session ID
            run_id: AgentRun ID

        Returns:
            文件路径
        """
        run_dir = self._base_dir / "session" / session_id / "agent_runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / f"{run_id}.json"

    async def save(self, run: AgentRun, session_id: str) -> None:
        """保存 AgentRun 到磁盘。

        Args:
            run: AgentRun 实例
            session_id: 所属 Session ID
        """
        import aiofiles
        path: Path = self._run_path(session_id, run.run_id)
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(
                run.to_dict(), ensure_ascii=False, indent=2
            ))

    async def load(self, run_id: str, session_id: str) -> AgentRun | None:
        """加载 AgentRun。返回 None 如果不存在。

        Args:
            run_id: AgentRun ID
            session_id: 所属 Session ID
        """
        import aiofiles
        path: Path = self._run_path(session_id, run_id)
        if not path.exists():
            return None
        try:
            async with aiofiles.open(path, encoding="utf-8") as f:
                data: str = await f.read()
            return AgentRun.from_dict(json.loads(data))
        except Exception:
            return None
