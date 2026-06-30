"""Memory 模块 —— 记忆管理。

长期记忆 (L2) 由 MemoryManager 管理。
对话持久化已迁移到 dotclaw.storage.conversation。
Session 已迁移到 dotclaw.session。
"""

from .manager import MemoryManager

__all__ = ["MemoryManager"]
