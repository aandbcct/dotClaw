"""Skill 注册表 — Phase 7"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import SkillMeta

logger = logging.getLogger("dotclaw.skills.registry")


class SkillRegistry:
    """Skill 元数据注册表。"""

    def __init__(self):
        self._index: dict[str, "SkillMeta"] = {}

    def register(self, meta: "SkillMeta") -> None:
        """注册 Skill。同名后注册覆盖前注册（静默覆盖）。"""
        # M3 修复：debug 级别覆盖日志
        if meta.name in self._index:
            logger.debug(f"Skill 覆盖注册: {meta.name}")
        self._index[meta.name] = meta

    def get(self, name: str) -> "SkillMeta | None":
        """按名称获取 Skill 元数据。"""
        return self._index.get(name)

    def list_all(self) -> list["SkillMeta"]:
        """返回所有已注册的 Skill 元数据。"""
        return list(self._index.values())

    def get_descriptions_block(self, max_desc_len: int = 40) -> str:
        """生成注入 system prompt 的 Skill 描述列表。"""
        # M4 修复：默认 max_desc_len 从 20 提升到 40（兼容 CJK 字符）
        if not self._index:
            return ""

        lines = []
        for meta in sorted(self._index.values(), key=lambda m: m.name):
            # M2 修复：使用 SkillMeta.truncated_description 共享方法
            first_line = meta.truncated_description(max_len=max_desc_len)
            location = str(meta.skill_md_path)
            lines.append(f"- **{meta.name}**: {first_line} `{location}`")

        return "\n".join(lines)
