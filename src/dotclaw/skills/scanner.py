"""Skill 扫描器 — Phase 7"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from .models import SkillMeta, SkillLifecycle

logger = logging.getLogger("dotclaw.skills.scanner")


class SkillScanner:
    """递归扫描 skills 目录，构建 SkillMeta 列表。"""

    def __init__(self, skill_paths: list[str | Path], skip_prefix: str = "_"):
        self._skill_paths = [Path(p) for p in skill_paths]
        self._skip_prefix = skip_prefix

    def scan(self) -> list[SkillMeta]:
        """扫描所有 skill 路径，返回 SkillMeta 列表"""
        results: list[SkillMeta] = []
        seen_names: set[str] = set()

        for base_path in self._skill_paths:
            if not base_path.exists():
                logger.debug(f"Skill 目录不存在: {base_path}")
                continue

            for skill_md in self._find_skill_files(base_path):
                meta = self._parse_skill(skill_md)
                if meta is None:
                    continue

                if meta.name in seen_names:
                    logger.warning(f"Skill 名称重复，跳过: {meta.name} ({meta.skill_dir})")
                    continue

                seen_names.add(meta.name)
                results.append(meta)

        logger.info(f"Skill 扫描完成：共 {len(results)} 个 Skill")
        return results

    def _find_skill_files(self, base_path: Path) -> list[Path]:
        """递归查找所有 SKILL.md，跳过 _ 前缀目录"""
        results: list[Path] = []

        def _walk(path: Path):
            try:
                for entry in path.iterdir():
                    # W1 修复：follow_symlinks=False 防止符号链接循环导致 RecursionError
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.name.startswith(self._skip_prefix):
                        continue
                    skill_md = entry / "SKILL.md"
                    if skill_md.exists():
                        results.append(skill_md)
                    _walk(entry)
            except PermissionError:
                logger.warning(f"无权限访问: {path}")

        _walk(base_path)
        return results

    def _parse_skill(self, skill_md: Path) -> SkillMeta | None:
        """解析单个 SKILL.md，构建 SkillMeta"""
        skill_dir = skill_md.parent

        try:
            content = skill_md.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"读取 SKILL.md 失败: {skill_md} — {e}")
            return None

        fm = self._parse_frontmatter(content)
        if fm is None:
            logger.warning(f"SKILL.md 无有效 frontmatter: {skill_md}")
            return None

        if not fm.get("name"):
            logger.warning(f"SKILL.md 缺少 name 字段: {skill_md}")
            return None

        # M5 修复：description 为空时的 hint 日志
        if not fm.get("description", "").strip():
            logger.debug(f"Skill {fm.get('name')} 的 description 为空，LLM 匹配可能受到影响")

        script_paths = self._scan_subdir(skill_dir, "scripts")
        reference_paths = self._scan_subdir(skill_dir, "references")

        metadata = fm.get("metadata", {})
        openclaw = metadata.get("openclaw", {}) if isinstance(metadata, dict) else {}

        lifecycle = self._parse_lifecycle(fm.get("lifecycle", "persistent"))

        deactivate_raw = fm.get("deactivate_on", [])
        deactivate_on = tuple(deactivate_raw) if isinstance(deactivate_raw, list) else ()

        keywords_raw = fm.get("keywords", [])
        keywords = tuple(keywords_raw) if isinstance(keywords_raw, list) else ()

        known_keys = {
            "name", "description", "keywords", "lifecycle",
            "deactivate_on", "homepage", "author", "metadata",
        }
        extra = {k: v for k, v in fm.items() if k not in known_keys}

        return SkillMeta(
            name=fm["name"],
            description=fm.get("description", ""),
            keywords=keywords,
            lifecycle=lifecycle,
            deactivate_on=deactivate_on,
            always_load=bool(openclaw.get("always", False)),
            emoji=str(openclaw.get("emoji", "")),
            homepage=str(fm.get("homepage", "")),
            author=str(fm.get("author", "")),
            metadata=metadata if isinstance(metadata, dict) else {},
            extra=extra,
            skill_dir=skill_dir,
            skill_md_path=skill_md,
            has_scripts=len(script_paths) > 0,
            has_references=len(reference_paths) > 0,
            script_paths=tuple(script_paths),
            reference_paths=tuple(reference_paths),
        )

    def _parse_frontmatter(self, content: str) -> dict[str, Any] | None:
        """用 yaml.safe_load() 解析 YAML frontmatter"""
        # M1 修复：规范化所有换行符格式（\r\n Windows + \r 旧 Mac + \n Unix）
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if not match:
            return None
        try:
            return yaml.safe_load(match.group(1))
        except yaml.YAMLError as e:
            logger.warning(f"YAML 解析失败: {e}")
            return None

    def _parse_lifecycle(self, value: Any) -> SkillLifecycle:
        """解析 lifecycle 枚举，无效值降级为 PERSISTENT"""
        if isinstance(value, SkillLifecycle):
            return value
        try:
            return SkillLifecycle(str(value))
        except ValueError:
            logger.warning(f"无效 lifecycle 值: {value}，降级为 PERSISTENT")
            return SkillLifecycle.PERSISTENT

    def _scan_subdir(self, skill_dir: Path, subdir_name: str) -> list[str]:
        """扫描 scripts/ 或 references/ 子目录，返回相对路径列表"""
        subdir = skill_dir / subdir_name
        # W1 修复：follow_symlinks=False + is_symlink 检查
        if not subdir.is_dir(follow_symlinks=False):
            return []
        paths: list[str] = []
        for f in subdir.rglob("*"):
            if f.is_file() and not f.is_symlink():
                paths.append(str(f.relative_to(skill_dir)))
        return sorted(paths)
