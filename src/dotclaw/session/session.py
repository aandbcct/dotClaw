"""Session —— 持久化对话记录。

Session 是 dotClaw 的对话隔离单元：一个 Session 文件 = 一段独立对话，
不同 Session 之间的对话记录互相隔离。

结构：
  持久化字段 → 存 JSON 文件（id/title/conversations/model/...）
  Runtime 在 run() 内部管理当前执行周期的消息历史（all_messages）
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


# ============================================================================
# Conversation — 一条请求的持久化记录
# ============================================================================

@dataclass
class Conversation:
    """Session 中的一条对话记录。

    一次用户请求 → 一条 Conversation 记录。
    一条请求可能产生多个 AgentRun（父 spawn 子），agent_run_ids 记录所有。

    字段：
        user_query: 用户输入文本
        final_answer: Agent 最终回答文本
        agent_run_ids: 本次请求产生的所有 AgentRun ID 列表
        created_at: 记录创建时间
    """

    user_query: str
    """用户输入文本"""

    final_answer: str = ""
    """Agent 最终回答文本"""

    agent_run_ids: list[str] = field(default_factory=list)
    """本次请求产生的所有 AgentRun ID（有序）"""

    created_at: str = ""
    """记录创建时间（ISO 8601）"""


# ============================================================================
# Session
# ============================================================================

@dataclass
class Session:
    """对话隔离单元 —— 持久化记录。

    持久化字段（存磁盘）：
        id / title / agent_id / model / created_at / updated_at / conversations
    """

    # ── 持久化字段 ──

    id: str
    """Session 唯一标识（8 位 hex）"""

    title: str = "新对话"
    """Session 标题"""

    agent_id: str = ""
    """关联的 Agent ID"""

    model: str = ""
    """创建时使用的模型名"""

    created_at: str = ""
    """创建时间（ISO 8601）"""

    updated_at: str = ""
    """最后更新时间（ISO 8601）"""

    conversations: list[Conversation] = field(default_factory=list)
    """对话记录列表。每条 = 一次用户请求的完整记录。"""

    # ── 序列化 ──

    def to_dict(self) -> dict:
        """序列化为 dict（仅持久化字段）。"""
        d: dict = asdict(self)
        # 转换 conversations 中的 Conversation 对象
        d["conversations"] = [asdict(c) for c in self.conversations]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Session:
        """从 dict 反序列化。"""
        convs_data: list[dict] = data.pop("conversations", [])
        session: Session = cls(**data)
        session.conversations = [
            Conversation(**c) for c in convs_data
        ]
        return session

    def add_conversation(self, user_query: str, final_answer: str,
                         agent_run_ids: list[str]) -> Conversation:
        """追加一条对话记录。

        Args:
            user_query: 用户输入
            final_answer: Agent 回答
            agent_run_ids: 关联的 AgentRun ID 列表

        Returns:
            新创建的 Conversation 记录
        """
        conv: Conversation = Conversation(
            user_query=user_query,
            final_answer=final_answer,
            agent_run_ids=list(agent_run_ids),
            created_at=datetime.now().isoformat(),
        )
        self.conversations.append(conv)
        return conv


# ============================================================================
# SessionManager
# ============================================================================

class SessionManager:
    """Session 持久化管理器。

    每个 Session 存储为独立 JSON 文件：{data_dir}/session/{session_id}/session.json
    """

    def __init__(self, data_dir: str | Path) -> None:
        """初始化。

        Args:
            data_dir: 数据目录路径
        """
        import dotclaw
        module_path: Path = Path(dotclaw.__file__).parent  # src/dotclaw/
        project_root: Path = module_path.parent.parent  # 项目根目录
        self._data_dir: Path = (project_root / data_dir).resolve()
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        """获取 Session 文件路径。"""
        session_dir: Path = self._data_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir / "session.json"

    async def create(self, title: str = "新对话", model: str = "",
                     agent_id: str = "") -> Session:
        """创建新 Session 并持久化。"""
        import uuid
        now: str = datetime.now().isoformat()
        session: Session = Session(
            id=str(uuid.uuid4())[:8],
            title=title,
            agent_id=agent_id,
            model=model,
            created_at=now,
            updated_at=now,
        )
        await self.save(session)
        return session

    async def load(self, session_id: str) -> Session | None:
        """加载 Session。返回 None 如果不存在。"""
        path: Path = self._session_path(session_id)
        if not path.exists():
            return None
        try:
            import aiofiles
            async with aiofiles.open(path, encoding="utf-8") as f:
                data: str = await f.read()
            return Session.from_dict(json.loads(data))
        except Exception:
            return None

    async def save(self, session: Session) -> None:
        """保存 Session 到磁盘。"""
        session.updated_at = datetime.now().isoformat()
        path: Path = self._session_path(session.id)
        import aiofiles
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(
                session.to_dict(), ensure_ascii=False, indent=2
            ))

    async def list_all(self) -> list[Session]:
        """列出所有 Session（按更新时间倒序）。"""
        sessions: list[Session] = []
        for d in self._data_dir.iterdir():
            if not d.is_dir():
                continue
            path: Path = d / "session.json"
            if not path.exists():
                continue
            try:
                import aiofiles
                async with aiofiles.open(path, encoding="utf-8") as f:
                    data: str = await f.read()
                sessions.append(Session.from_dict(json.loads(data)))
            except Exception:
                pass
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    async def delete(self, session_id: str) -> bool:
        """删除 Session。"""
        path: Path = self._session_path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False
