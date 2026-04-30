"""Skill 加载模块"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Skill:
    """解析后的 Skill 对象"""
    name: str
    description: str
    directory: Path
    instructions: str  # SKILL.md 正文内容


class SkillLoader:
    """
    加载 skills/ 目录下所有 SKILL.md。

    SKILL.md 格式（YAML 头部 + Markdown 正文）:
        ---
        name: weather
        description: "查询天气..."
        ---
        # 天气技能
        正文...
    """

    def load_all(self, skills_dir: str | Path) -> list[Skill]:
        """加载目录下所有 skill"""
        skills_dir = Path(skills_dir)
        if not skills_dir.exists():
            return []

        skills = []
        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                skill = self._parse(skill_md, skill_dir)
                if skill:
                    skills.append(skill)
        return skills

    def _parse(self, path: Path, skill_dir: Path) -> Skill | None:
        """解析单个 SKILL.md"""
        try:
            content = path.read_text(encoding="utf-8")

            # 提取 YAML 头部
            yaml_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
            if not yaml_match:
                return None

            yaml_content = yaml_match.group(1)
            body = content[yaml_match.end():].strip()

            # 简单解析 YAML
            name = ""
            desc = ""

            for line in yaml_content.splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"').strip("'")

            if not name:
                return None

            return Skill(
                name=name,
                description=desc,
                directory=skill_dir,
                instructions=body,
            )
        except Exception:
            return None

    def build_skill_prompt(self, skills: list[Skill]) -> str:
        """
        生成注入 system prompt 的技能描述段。
        """
        if not skills:
            return ""

        lines = ["", "## 可用技能", ""]
        for skill in skills:
            lines.append(f"### {skill.name}")
            lines.append(f"{skill.description}")
            lines.append("")

        lines.append("当需要使用某个技能时，优先按该技能的 instructions 执行。")
        return "\n".join(lines)
