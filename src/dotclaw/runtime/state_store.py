"""StateStore —— AgentState 结构化持久化。

职责：
- 将 AgentState 的关键字段序列化为 JSON
- 原子覆盖写入到 session/{session_id}/state.json
- 从持久化文件恢复 AgentState 快照

内部逻辑（原子操作封装为方法）：
1. save() — 将 AgentState 快照序列化并原子写入文件
2. load() — 从文件反序列化恢复 AgentState 快照
3. _ensure_dir() — 确保存储目录存在
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent_state import AgentPhase, AgentStatus

logger = logging.getLogger("dotclaw.runtime.state_store")


# ============================================================================
# StateSnapshot —— 可序列化的状态快照
# ============================================================================

@dataclass
class StateSnapshot:
    """AgentState 的可序列化快照。

    只包含需要跨 AgentRun 持久化的字段，不包含运行时引用（LLM/Tool）。
    """

    task_id: str
    """任务唯一标识"""

    thread_id: str
    """所属 Session ID"""

    agent_id: str
    """执行 Agent ID"""

    phase: str
    """当前执行阶段（AgentPhase.value）"""

    iteration: int
    """当前迭代次数"""

    max_iterations: int
    """最大迭代次数"""

    end_status: str
    """AgentRun 结束状态（AgentStatus.value）"""

    error_message: str | None = None
    """错误信息"""

    handoff_target: str | None = None
    """Handoff 目标"""

    handoff_context: str | None = None
    """Handoff 上下文"""

    tool_calls_total: int = 0
    """累计工具调用次数"""

    tasks: list[dict] = field(default_factory=list)
    """子任务清单（序列化后的 Task 数据）"""

    @classmethod
    def from_agent_state(cls, state: object) -> StateSnapshot:
        """从 AgentState 实例构建快照。

        Args:
            state: AgentState 实例

        Returns:
            StateSnapshot 实例
        """
        from .agent_state import AgentState
        from .task import Task

        as_obj: AgentState = state  # type: ignore[assignment]

        tasks_data: list[dict] = []
        for t in as_obj.tasks:
            tasks_data.append({
                "task_id": t.task_id,
                "description": t.description,
                "progress": t.progress.value,
                "parent_task_id": t.parent_task_id,
                "agent_id": t.agent_id,
                "agent_run_ids": list(t.agent_run_ids),
                "result": t.result,
                "error": t.error,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
            })

        return cls(
            task_id=as_obj.task_id,
            thread_id=as_obj.thread_id,
            agent_id=as_obj.agent_id,
            phase=as_obj.phase.value,
            iteration=as_obj.iteration,
            max_iterations=as_obj.max_iterations,
            end_status=as_obj.end_status.value,
            error_message=as_obj.error_message,
            handoff_target=as_obj.handoff_target,
            handoff_context=as_obj.handoff_context,
            tool_calls_total=as_obj.tool_calls_total,
            tasks=tasks_data,
        )

    def restore_to(self, state: object) -> None:
        """将快照数据恢复到 AgentState 实例。

        只恢复 base fields，不恢复 tasks（tasks 由独立 Task 管理）。

        Args:
            state: AgentState 实例
        """
        from .agent_state import AgentPhase, AgentStatus

        as_obj: object = state
        as_obj.iteration = self.iteration  # type: ignore[attr-defined]
        as_obj.end_status = AgentStatus(self.end_status)  # type: ignore[attr-defined]
        as_obj.error_message = self.error_message  # type: ignore[attr-defined]
        as_obj.handoff_target = self.handoff_target  # type: ignore[attr-defined]
        as_obj.handoff_context = self.handoff_context  # type: ignore[attr-defined]

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容的 dict。"""
        return {
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "agent_id": self.agent_id,
            "phase": self.phase,
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
            "end_status": self.end_status,
            "error_message": self.error_message,
            "handoff_target": self.handoff_target,
            "handoff_context": self.handoff_context,
            "tool_calls_total": self.tool_calls_total,
            "tasks": self.tasks,
        }

    @classmethod
    def from_dict(cls, data: dict) -> StateSnapshot:
        """从 dict 反序列化。"""
        return cls(
            task_id=data.get("task_id", ""),
            thread_id=data.get("thread_id", ""),
            agent_id=data.get("agent_id", ""),
            phase=data.get("phase", "idle"),
            iteration=data.get("iteration", 0),
            max_iterations=data.get("max_iterations", 10),
            end_status=data.get("end_status", "running"),
            error_message=data.get("error_message"),
            handoff_target=data.get("handoff_target"),
            handoff_context=data.get("handoff_context"),
            tool_calls_total=data.get("tool_calls_total", 0),
            tasks=data.get("tasks", []),
        )


# ============================================================================
# StateStore —— 持久化管理器
# ============================================================================

class StateStore:
    """AgentState 的结构化持久化管理器。

    使用临时文件 + 替换策略保证写入原子性。
    存储位置：{data_dir}/session/{session_id}/state.json

    Args:
        data_dir: 数据根目录（如 ./data/sessions）
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir: Path = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)

    # ======================== 原子操作：路径计算 ========================

    def _state_path(self, session_id: str) -> Path:
        """获取 state.json 文件路径。

        原子操作：
        1. 拼接 session/{session_id}/state.json
        2. 确保父目录存在

        Args:
            session_id: Session ID

        Returns:
            文件路径
        """
        path: Path = self._data_dir / session_id / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    # ======================== 原子操作：保存 ========================

    async def save(self, session_id: str, snapshot: StateSnapshot) -> None:
        """保存状态快照到磁盘（原子写入）。

        原子操作：
        1. 序列化 StateSnapshot 为 JSON
        2. 写入临时文件
        3. 原子替换目标文件

        Args:
            session_id: Session ID
            snapshot: 状态快照
        """
        import aiofiles
        import tempfile
        import os as _os

        target: Path = self._state_path(session_id)
        data: str = json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2)

        try:
            # 写入临时文件（同目录，保证原子 rename）
            fd, tmp_path = tempfile.mkstemp(
                suffix=".tmp",
                prefix="state_",
                dir=str(target.parent),
            )
            try:
                _os.write(fd, data.encode("utf-8"))
            finally:
                _os.close(fd)
            # 原子替换
            Path(tmp_path).replace(target)
        except OSError as e:
            logger.error(f"Failed to save state for session '{session_id}': {e}")

    # ======================== 原子操作：加载 ========================

    async def load(self, session_id: str) -> StateSnapshot | None:
        """加载状态快照。

        原子操作：
        1. 读取 state.json 文件
        2. 反序列化为 StateSnapshot

        Args:
            session_id: Session ID

        Returns:
            StateSnapshot 或 None（不存在或读取失败）
        """
        import aiofiles

        target: Path = self._state_path(session_id)
        if not target.is_file():
            return None

        try:
            async with aiofiles.open(target, encoding="utf-8") as f:
                data: str = await f.read()
            return StateSnapshot.from_dict(json.loads(data))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load state for session '{session_id}': {e}")
            return None

    # ======================== 原子操作：删除 ========================

    async def delete(self, session_id: str) -> None:
        """删除状态快照文件。

        原子操作：
        1. 查找 state.json 文件
        2. 如果存在则删除

        Args:
            session_id: Session ID
        """
        target: Path = self._state_path(session_id)
        if target.is_file():
            try:
                target.unlink()
            except OSError as e:
                logger.warning(f"Failed to delete state for session '{session_id}': {e}")
