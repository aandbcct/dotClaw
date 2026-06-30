"""Conversation —— 持久化对话记录。

从 memory/store.py 的 Session 重命名而来。
Conversation 是应用层概念：用户可见的持久化对话记录。
Session 是运行时概念：Agent 执行时的易失性上下文。

命名映射：
  原 SessionMessage → ConversationMessage
  原 Session        → Conversation
  原 SessionManager → ConversationManager
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import aiofiles


def _resolve_data_dir(relative_path: str | Path) -> Path:
    """将相对路径解析为相对于项目根目录（config.yaml 所在目录）。"""
    import dotclaw
    module_path = Path(dotclaw.__file__).parent  # src/dotclaw/
    project_root = module_path.parent.parent  # 项目根目录
    resolved = project_root / relative_path
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


@dataclass
class ConversationMessage:
    """对话中的一条持久化消息。

    与 Message (llm.base.Message) 不同：ConversationMessage 是面向存储的简化结构，
    只包含 role/content/name/tool_call_id，不含 tool_calls 等复杂嵌套。
    """

    role: str
    """消息角色：user / assistant / system"""

    content: str
    """消息文本内容"""

    name: str | None = None
    """可选的消息发送者名称"""

    tool_call_id: str | None = None
    """工具调用 ID（仅 role=tool 时使用）"""


@dataclass
class Conversation:
    """持久化对话记录。

    一个 Conversation 存储为独立的 JSON 文件。
    1 个 Session (运行时) 加载 1 个 Conversation (持久化)。
    1 个 Conversation 可被多个 Session 加载（如续接历史对话）。
    """

    id: str
    """对话唯一标识（8 位 hex）"""

    title: str
    """对话标题"""

    created_at: str
    """创建时间（ISO 8601）"""

    updated_at: str
    """最后更新时间（ISO 8601）"""

    messages: list[ConversationMessage] = field(default_factory=list)
    """对话消息列表（持久化记录）"""

    model: str = "qwen-plus"
    """创建时使用的模型名"""

    summary: str | None = None
    """对话摘要（可选，由 DeepDream 生成）"""

    def to_dict(self) -> dict:
        """将 Conversation 序列化为 dict。"""
        return asdict(self)


class ConversationManager:
    """对话持久化管理器。

    每个对话存储为独立的 JSON 文件：{data_dir}/{id}.json。
    """

    def __init__(self, data_dir: str | Path) -> None:
        """初始化对话管理器。

        Args:
            data_dir: 数据目录（相对或绝对路径）
        """
        self._data_dir: Path = _resolve_data_dir(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)

    # ── 内部方法 ──

    @staticmethod
    def _dict_to_conversation(raw: dict) -> Conversation:
        """将 JSON 反序列化的 dict 转为 Conversation。

        确保 messages 字段中的元素是 ConversationMessage 对象。
        """
        messages: list[ConversationMessage] = []
        for m in raw.pop("messages", []):
            if isinstance(m, ConversationMessage):
                messages.append(m)
            elif isinstance(m, dict):
                messages.append(ConversationMessage(**m))
        raw["messages"] = messages
        return Conversation(**raw)

    def _conversation_path(self, conversation_id: str) -> Path:
        """获取对话文件的完整路径。"""
        return self._data_dir / f"{conversation_id}.json"

    # ── 公开 API ──

    async def create(self, title: str = "新对话", model: str = "qwen-plus") -> Conversation:
        """创建新对话并持久化。

        Args:
            title: 对话标题
            model: 模型名

        Returns:
            新建的 Conversation
        """
        import uuid
        conversation: Conversation = Conversation(
            id=str(uuid.uuid4())[:8],
            title=title,
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            model=model,
        )
        await self.save(conversation)
        return conversation

    async def load(self, conversation_id: str) -> Conversation | None:
        """加载指定对话。

        Args:
            conversation_id: 对话 ID

        Returns:
            Conversation 实例，不存在时返回 None
        """
        path: Path = self._conversation_path(conversation_id)
        if not path.exists():
            return None
        try:
            async with aiofiles.open(path, encoding="utf-8") as f:
                data: str = await f.read()
            return self._dict_to_conversation(json.loads(data))
        except Exception:
            return None

    async def save(self, conversation: Conversation) -> None:
        """保存对话到磁盘。

        自动更新 updated_at 时间戳。
        """
        conversation.updated_at = datetime.now().isoformat()
        path: Path = self._conversation_path(conversation.id)
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(conversation.to_dict(), ensure_ascii=False, indent=2))

    async def list_all(self) -> list[Conversation]:
        """列出所有对话（按更新时间倒序）。

        Returns:
            对话列表
        """
        conversations: list[Conversation] = []
        for path in self._data_dir.glob("*.json"):
            try:
                async with aiofiles.open(path, encoding="utf-8") as f:
                    data: str = await f.read()
                conversations.append(self._dict_to_conversation(json.loads(data)))
            except Exception:
                pass
        conversations.sort(key=lambda c: c.updated_at, reverse=True)
        return conversations

    async def delete(self, conversation_id: str) -> bool:
        """删除指定对话。

        Args:
            conversation_id: 对话 ID

        Returns:
            True 表示已删除，False 表示文件不存在
        """
        path: Path = self._conversation_path(conversation_id)
        if path.exists():
            path.unlink()
            return True
        return False
