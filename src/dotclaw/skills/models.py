"""Skill 数据模型 — Phase 7"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class SkillLifecycle(StrEnum):
    """Skill 生命周期模式

    P7 注意：ONE_SHOT 和 EPHEMERAL 仅定义不消费。
    生命周期管理留待后续 Phase。
    """
    PERSISTENT = "persistent"   # 持久：触发后一直活跃（P7 唯一消费的值）
    ONE_SHOT = "one-shot"       # 一次性：完成后自动卸载（预留）
    EPHEMERAL = "ephemeral"     # 临时：每次请求重新判定（预留）


@dataclass(frozen=True)
class SkillMeta:
    """Skill 完整元数据，始终常驻内存。"""

    # ── frontmatter 基础字段 ──
    name: str
    description: str

    # ── frontmatter 扩展字段 ──
    keywords: tuple[str, ...] = ()
    lifecycle: SkillLifecycle = SkillLifecycle.PERSISTENT
    deactivate_on: tuple[str, ...] = ()
    always_load: bool = False
    emoji: str = ""
    homepage: str = ""
    author: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    # ── 文件系统字段 ──
    skill_dir: Path = field(default_factory=Path)
    skill_md_path: Path = field(default_factory=Path)
    has_scripts: bool = False
    has_references: bool = False
    script_paths: tuple[str, ...] = ()
    reference_paths: tuple[str, ...] = ()

    def truncated_description(self, max_len: int = 40) -> str:
        """M2 修复：共享的截断描述方法。取第一行，超过 max_len 截断加 ..."""
        first_line = self.description.split("\n")[0].strip()
        if len(first_line) > max_len:
            first_line = first_line[:max_len] + "..."
        return first_line
