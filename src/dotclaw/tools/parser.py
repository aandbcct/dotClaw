"""SkillParser — 工具执行后检测 skill 相关操作。

通过分析 read_file / exec 的参数，判断本次调用是否命中了 skill 的
body（SKILL.md）、reference 文件或 script。与 ToolExecutor 协作，
不侵入 AgentLoop。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dotclaw.skills.registry import SkillRegistry
    from dotclaw.skills.models import SkillMeta

logger = logging.getLogger("dotclaw.tools.parser")


class SkillParser:
    """工具调用参数解析器——检测 read_file / exec 是否命中 skill。

    Args:
        registry: SkillRegistry，提供已注册 skill 的元数据。
    """

    def __init__(self, registry: "SkillRegistry | None") -> None:
        self._registry = registry
        # 构建快速查找表：skill_dir → SkillMeta
        self._skill_by_dir: dict[Path, "SkillMeta"] = {}
        if registry:
            for meta in registry.list_all():
                skill_dir = meta.skill_dir.resolve() if meta.skill_dir else None
                if skill_dir:
                    self._skill_by_dir[skill_dir] = meta

    def parse(self, tool_name: str, args: dict) -> tuple[str, str, str] | None:
        """解析工具调用，返回 (skill_name, part, osname) 或 None。

        part: "body" | "reference" | "script"
        osname: 对于 reference 是文件名，对于 script 是脚本路径
        """
        if not self._skill_by_dir:
            return None

        path = args.get("path") or args.get("file_path") or ""

        # 1. 检查路径是否在任意 skill 目录下
        resolved = self._resolve(path)
        if resolved is None:
            return None

        skill_meta = self._find_skill(resolved)
        if skill_meta is None:
            return None

        # 2. 根据工具名和路径判断操作类型
        if tool_name in ("read_file", "read"):
            if resolved.name == "SKILL.md" or path.endswith("SKILL.md"):
                return (skill_meta.name, "body", "")
            else:
                # reference 文件
                return (skill_meta.name, "reference", resolved.name)

        elif tool_name in ("exec", "python", "bash"):
            # 脚本执行
            return (skill_meta.name, "script", path)

        return None

    def _resolve(self, path: str) -> Path | None:
        """将路径解析为绝对路径。非绝对路径或不存在则返回 None。"""
        try:
            p = Path(path).resolve()
            if p.exists():
                return p
        except Exception:
            pass
        return None

    def _find_skill(self, resolved: Path) -> "SkillMeta | None":
        """根据已解析的绝对路径查找所属 skill。"""
        current = resolved.parent
        max_depth = 5  # 最多向上找 5 层
        for _ in range(max_depth):
            meta = self._skill_by_dir.get(current)
            if meta is not None:
                return meta
            if current.parent == current:
                break
            current = current.parent
        return None
