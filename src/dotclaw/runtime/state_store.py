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

from .adapters.file_checkpoint_repository import FileCheckpointRepository
from .agent_state import AgentState, AgentStatus
from .domain.models import JSONMap, JSONValue, RunCheckpoint, get_integer, get_string, require_json_map

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

    truncated_count: int = 0
    """TRUNCATED 续跑次数（防无限续跑）"""

    retry_count: int = 0
    """工具重试次数（RETRYING 流）"""

    max_tool_retries: int = 2
    """工具执行最大重试次数"""

    tasks: list[JSONMap] = field(default_factory=list)
    """子任务清单（序列化后的 Task 数据）"""

    @classmethod
    def from_agent_state(cls, state: AgentState) -> StateSnapshot:
        """从 AgentState 实例构建快照。

        Args:
            state: AgentState 实例

        Returns:
            StateSnapshot 实例
        """
        tasks_data: list[JSONMap] = []
        for task in state.tasks:
            tasks_data.append({
                "task_id": task.task_id,
                "description": task.description,
                "progress": task.progress.value,
                "parent_task_id": task.parent_task_id,
                "agent_id": task.agent_id,
                "agent_run_ids": list(task.agent_run_ids),
                "result": task.result,
                "error": task.error,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            })

        return cls(
            task_id=state.task_id,
            thread_id=state.thread_id,
            agent_id=state.agent_id,
            phase=state.phase.value,
            iteration=state.iteration,
            max_iterations=state.max_iterations,
            end_status=state.end_status.value,
            error_message=state.error_message,
            handoff_target=state.handoff_target,
            handoff_context=state.handoff_context,
            tool_calls_total=state.tool_calls_total,
            truncated_count=state.truncated_count,
            retry_count=state.retry_count,
            max_tool_retries=state.max_tool_retries,
            tasks=tasks_data,
        )

    def restore_to(self, state: AgentState) -> None:
        """将快照数据恢复到 AgentState 实例。

        只恢复 base fields，不恢复 tasks（tasks 由独立 Task 管理）。

        Args:
            state: AgentState 实例
        """
        state.iteration = self.iteration
        state.end_status = AgentStatus(self.end_status)
        state.error_message = self.error_message
        state.handoff_target = self.handoff_target
        state.handoff_context = self.handoff_context
        state.truncated_count = self.truncated_count
        state.retry_count = self.retry_count
        state.max_tool_retries = self.max_tool_retries

    def to_dict(self) -> JSONMap:
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
            "truncated_count": self.truncated_count,
            "retry_count": self.retry_count,
            "max_tool_retries": self.max_tool_retries,
            "tasks": self.tasks,
        }

    @classmethod
    def from_dict(cls, data: JSONMap) -> StateSnapshot:
        """从 dict 反序列化。"""
        raw_tasks: JSONValue | None = data.get("tasks")
        tasks: list[JSONMap] = []
        if isinstance(raw_tasks, list):
            raw_task: JSONValue
            for raw_task in raw_tasks:
                tasks.append(require_json_map(raw_task))
        return cls(
            task_id=get_string(data, "task_id"),
            thread_id=get_string(data, "thread_id"),
            agent_id=get_string(data, "agent_id"),
            phase=get_string(data, "phase", "idle"),
            iteration=get_integer(data, "iteration"),
            max_iterations=get_integer(data, "max_iterations", 10),
            end_status=get_string(data, "end_status", "running"),
            error_message=_optional_string(data.get("error_message")),
            handoff_target=_optional_string(data.get("handoff_target")),
            handoff_context=_optional_string(data.get("handoff_context")),
            tool_calls_total=get_integer(data, "tool_calls_total"),
            truncated_count=get_integer(data, "truncated_count"),
            retry_count=get_integer(data, "retry_count"),
            max_tool_retries=get_integer(data, "max_tool_retries", 2),
            tasks=tasks,
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
        self._checkpoint_repository: FileCheckpointRepository = FileCheckpointRepository(self._data_dir)

    async def save_checkpoint(self, checkpoint: RunCheckpoint) -> None:
        """委托 v2 CheckpointRepository 按 run_id 保存恢复点。

        旧 save() 保持 Session 级兼容写入，RuntimeEngine 后续只能调用本方法。
        """
        await self._checkpoint_repository.save(checkpoint)

    async def load_checkpoint(self, session_id: str, run_id: str) -> RunCheckpoint | None:
        """委托 v2 CheckpointRepository 读取指定运行恢复点。"""
        return await self._checkpoint_repository.load(session_id, run_id)

    async def delete_checkpoint(self, session_id: str, run_id: str) -> None:
        """委托 v2 CheckpointRepository 删除指定运行恢复点。"""
        await self._checkpoint_repository.delete(session_id, run_id)

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
        """兼容旧调用方保存 Session 级状态快照。

        Runtime v2 新写入必须使用 save_checkpoint()；本方法仅为旧 Runtime
        保留，并委托给显式命名的 save_legacy()。
        """
        await self.save_legacy(session_id, snapshot)

    async def save_legacy(self, session_id: str, snapshot: StateSnapshot) -> None:
        """保存旧 Runtime 使用的 Session 级状态快照（原子写入）。

        原子操作：
        1. 序列化 StateSnapshot 为 JSON
        2. 写入临时文件
        3. 原子替换目标文件

        Args:
            session_id: Session ID
            snapshot: 状态快照
        """
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
        """兼容旧调用方加载 Session 级状态快照。"""
        return await self.load_legacy(session_id)

    async def load_legacy(self, session_id: str) -> StateSnapshot | None:
        """加载旧 Runtime 使用的 Session 级状态快照。

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
            raw_data: JSONValue = json.loads(data)
            return StateSnapshot.from_dict(require_json_map(raw_data))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load state for session '{session_id}': {e}")
            return None

    # ======================== 原子操作：删除 ========================

    async def delete(self, session_id: str) -> None:
        """兼容旧调用方删除 Session 级状态快照。"""
        await self.delete_legacy(session_id)

    async def delete_legacy(self, session_id: str) -> None:
        """删除旧 Runtime 使用的状态快照文件。

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


def _optional_string(value: JSONValue | None) -> str | None:
    """将可选 JSON 值收窄为字符串或 None。"""
    return value if isinstance(value, str) else None
