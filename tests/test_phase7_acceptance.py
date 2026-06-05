"""Phase 7 验收测试 — Skill 系统完善

覆盖：SkillMeta / SkillLifecycle / SkillScanner / SkillRegistry / SkillsProvider / SkillsConfig
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _make_skill_dir(base: Path, name: str, frontmatter: str, body: str = "") -> Path:
    """创建测试 Skill 目录"""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\n{frontmatter}\n---\n\n{body}"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


# ============================================================
# 场景 1: SkillMeta + SkillLifecycle
# ============================================================

class TestSkillMeta:
    """SkillMeta 数据模型测试"""

    def test_meta_creation(self):
        from dotclaw.skills.models import SkillMeta, SkillLifecycle

        meta = SkillMeta(
            name="test-skill",
            description="A test skill",
            lifecycle=SkillLifecycle.PERSISTENT,
        )
        assert meta.name == "test-skill"
        assert meta.description == "A test skill"
        assert meta.lifecycle == SkillLifecycle.PERSISTENT
        assert meta.keywords == ()

    def test_meta_frozen(self):
        from dotclaw.skills.models import SkillMeta

        meta = SkillMeta(name="test", description="desc")
        with pytest.raises(AttributeError):
            meta.name = "changed"  # type: ignore

    def test_lifecycle_enum_values(self):
        from dotclaw.skills.models import SkillLifecycle
        assert SkillLifecycle.PERSISTENT.value == "persistent"
        assert SkillLifecycle.ONE_SHOT.value == "one-shot"
        assert SkillLifecycle.EPHEMERAL.value == "ephemeral"

    def test_meta_fields(self):
        from dotclaw.skills.models import SkillMeta, SkillLifecycle
        p = Path("/test")
        meta = SkillMeta(
            name="full",
            description="Full test\nMulti line",
            keywords=("test", "demo"),
            lifecycle=SkillLifecycle.ONE_SHOT,
            deactivate_on=("done",),
            always_load=True,
            emoji="🚀",
            homepage="https://example.com",
            author="Tester",
            metadata={"key": "value"},
            extra={"custom": "field"},
            skill_dir=p,
            skill_md_path=p / "SKILL.md",
            has_scripts=True,
            has_references=False,
            script_paths=("scripts/run.py",),
            reference_paths=(),
        )
        assert meta.always_load is True
        assert meta.emoji == "🚀"
        assert meta.has_scripts is True
        assert meta.has_references is False
        assert meta.script_paths == ("scripts/run.py",)


# ============================================================
# 场景 2: SkillsConfig
# ============================================================

class TestSkillsConfig:
    """SkillsConfig 配置解析测试"""

    def test_config_defaults(self):
        from dotclaw.config.settings import SkillsConfig
        cfg = SkillsConfig()
        assert cfg.directory == "./skills"
        assert cfg.enabled is True
        assert cfg.skip_prefix == "_"

    def test_config_directory_list(self):
        from dotclaw.config.settings import SkillsConfig
        cfg = SkillsConfig(directory=["./skills", "./extra"])
        assert isinstance(cfg.directory, list)
        assert len(cfg.directory) == 2

    def test_raw_to_config_parses(self):
        from dotclaw.config.settings import _raw_to_config
        config = _raw_to_config({})
        assert config.skills.directory == "./skills"
        assert config.skills.enabled is True

    def test_raw_to_config_list(self):
        from dotclaw.config.settings import _raw_to_config
        config = _raw_to_config({
            "skills": {"directory": ["./skills", "./extra-skills"]}
        })
        assert isinstance(config.skills.directory, list)
        assert len(config.skills.directory) == 2


# ============================================================
# 场景 3: SkillScanner
# ============================================================

class TestSkillScanner:
    """SkillScanner 扫描测试"""

    def test_scan_basic(self):
        from dotclaw.skills.scanner import SkillScanner

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _make_skill_dir(base, "test-skill", "name: test-skill\ndescription: A test skill")
            _make_skill_dir(base, "other", "name: other\ndescription: Another skill")

            scanner = SkillScanner([str(base)])
            metas = scanner.scan()

            names = {m.name for m in metas}
            assert names == {"test-skill", "other"}
            assert all(m.skill_md_path.exists() for m in metas)

    def test_scan_skip_underscore_prefix(self):
        from dotclaw.skills.scanner import SkillScanner

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _make_skill_dir(base, "visible", "name: visible\ndescription: desc")
            _make_skill_dir(base, "_hidden", "name: hidden\ndescription: desc")

            scanner = SkillScanner([str(base)], skip_prefix="_")
            metas = scanner.scan()
            names = {m.name for m in metas}
            assert "visible" in names
            assert "hidden" not in names

    def test_scan_missing_directory(self):
        from dotclaw.skills.scanner import SkillScanner

        scanner = SkillScanner(["/nonexistent/path"])
        metas = scanner.scan()
        assert metas == []

    def test_scan_missing_name(self):
        from dotclaw.skills.scanner import SkillScanner

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _make_skill_dir(base, "no-name", "description: no name here")
            scanner = SkillScanner([str(base)])
            metas = scanner.scan()
            assert len(metas) == 0

    def test_scan_invalid_lifecycle_fallback(self):
        from dotclaw.skills.scanner import SkillScanner
        from dotclaw.skills.models import SkillLifecycle

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _make_skill_dir(base, "bad-lc", "name: bad-lc\ndescription: desc\nlifecycle: unknown")
            scanner = SkillScanner([str(base)])
            metas = scanner.scan()
            assert len(metas) == 1
            assert metas[0].lifecycle == SkillLifecycle.PERSISTENT

    def test_scan_recursive(self):
        from dotclaw.skills.scanner import SkillScanner

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            nested = base / "group" / "nested-skill"
            nested.mkdir(parents=True)
            (nested / "SKILL.md").write_text("---\nname: nested\ndescription: nested\n---\n")

            scanner = SkillScanner([str(base)])
            metas = scanner.scan()
            assert any(m.name == "nested" for m in metas)

    def test_scan_multiple_directories(self):
        from dotclaw.skills.scanner import SkillScanner

        with tempfile.TemporaryDirectory() as td1:
            with tempfile.TemporaryDirectory() as td2:
                _make_skill_dir(Path(td1), "a", "name: a\ndescription: A")
                _make_skill_dir(Path(td2), "b", "name: b\ndescription: B")

                scanner = SkillScanner([str(td1), str(td2)])
                metas = scanner.scan()
                names = {m.name for m in metas}
                assert names == {"a", "b"}

    def test_scan_duplicate_name_skipped(self):
        from dotclaw.skills.scanner import SkillScanner

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _make_skill_dir(base, "skill-a", "name: dup\ndescription: first")
            # Create a nested duplicate
            sub = base / "sub"
            sub.mkdir()
            _make_skill_dir(sub, "skill-b", "name: dup\ndescription: second")

            scanner = SkillScanner([str(base)])
            metas = scanner.scan()
            # Only one should be registered (first wins)
            dup_metas = [m for m in metas if m.name == "dup"]
            assert len(dup_metas) == 1

    def test_scan_frontmatter_multiline_description(self):
        from dotclaw.skills.scanner import SkillScanner

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            skill_dir = base / "multi"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: multi\ndescription: |\n  Line one\n  Line two\n---\n\nbody", encoding="utf-8"
            )
            scanner = SkillScanner([str(base)])
            metas = scanner.scan()
            assert len(metas) == 1
            assert "Line one" in metas[0].description

    def test_scan_with_scripts_references(self):
        from dotclaw.skills.scanner import SkillScanner

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            skill_dir = base / "full"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: full\ndescription: Has files\n---\n")

            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "run.py").write_text("print('hello')")

            refs_dir = skill_dir / "references"
            refs_dir.mkdir()
            (refs_dir / "guide.md").write_text("# Guide")

            scanner = SkillScanner([str(base)])
            metas = scanner.scan()
            assert len(metas) == 1
            assert metas[0].has_scripts is True
            assert metas[0].has_references is True
            assert len(metas[0].script_paths) == 1
            assert len(metas[0].reference_paths) == 1

    def test_scan_windows_line_endings(self):
        from dotclaw.skills.scanner import SkillScanner

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            skill_dir = base / "crlf"
            skill_dir.mkdir()
            content = "---\r\nname: crlf\r\ndescription: CRLF test\r\n---\r\n\r\nbody"
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

            scanner = SkillScanner([str(base)])
            metas = scanner.scan()
            assert len(metas) == 1
            assert metas[0].name == "crlf"

    def test_scan_keywords(self):
        from dotclaw.skills.scanner import SkillScanner

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _make_skill_dir(base, "kw", "name: kw\ndescription: keyword test\nkeywords:\n  - search\n  - web")

            scanner = SkillScanner([str(base)])
            metas = scanner.scan()
            assert len(metas) == 1
            assert metas[0].keywords == ("search", "web")


# ============================================================
# 场景 4: SkillRegistry
# ============================================================

class TestSkillRegistry:
    """SkillRegistry 测试"""

    def test_register_and_get(self):
        from dotclaw.skills.registry import SkillRegistry
        from dotclaw.skills.models import SkillMeta

        registry = SkillRegistry()
        meta = SkillMeta(name="test", description="desc")
        registry.register(meta)
        assert registry.get("test") is meta
        assert registry.get("nonexistent") is None

    def test_list_all(self):
        from dotclaw.skills.registry import SkillRegistry
        from dotclaw.skills.models import SkillMeta

        registry = SkillRegistry()
        registry.register(SkillMeta(name="a", description="A"))
        registry.register(SkillMeta(name="b", description="B"))
        assert len(registry.list_all()) == 2

    def test_duplicate_override(self):
        from dotclaw.skills.registry import SkillRegistry
        from dotclaw.skills.models import SkillMeta

        registry = SkillRegistry()
        meta1 = SkillMeta(name="test", description="first")
        meta2 = SkillMeta(name="test", description="second")
        registry.register(meta1)
        registry.register(meta2)
        assert registry.get("test") is meta2

    def test_descriptions_block_empty(self):
        from dotclaw.skills.registry import SkillRegistry

        registry = SkillRegistry()
        assert registry.get_descriptions_block() == ""

    def test_descriptions_block_format(self):
        from dotclaw.skills.registry import SkillRegistry
        from dotclaw.skills.models import SkillMeta
        from pathlib import Path

        registry = SkillRegistry()
        meta = SkillMeta(
            name="xbrowser",
            description="Browser automation skill",
            skill_dir=Path("/skills/xbrowser"),
            skill_md_path=Path("/skills/xbrowser/SKILL.md"),
        )
        registry.register(meta)

        block = registry.get_descriptions_block()
        assert "xbrowser" in block
        assert "Browser" in block
        assert "SKILL.md" in block


# ============================================================
# 场景 5: SkillsProvider
# ============================================================

class TestSkillsProvider:
    """SkillsProvider 测试"""

    def test_provide_none_when_null_registry(self):
        from dotclaw.agent.prompt.providers import SkillsProvider
        from dotclaw.agent.context import AgentContext
        from pathlib import Path

        provider = SkillsProvider()
        ctx = AgentContext(
            session_id="test",
            workspace=Path("."),
            project_root=Path("."),
            model="test",
            system_prompt="test",
        )
        result = provider.provide(ctx)
        assert result is None

    def test_provide_with_registry(self):
        from dotclaw.agent.prompt.providers import SkillsProvider
        from dotclaw.agent.context import AgentContext
        from dotclaw.skills.registry import SkillRegistry
        from dotclaw.skills.models import SkillMeta
        from pathlib import Path

        registry = SkillRegistry()
        meta = SkillMeta(
            name="hello",
            description="A test skill for demo",
            skill_dir=Path("/skills/hello"),
            skill_md_path=Path("/skills/hello/SKILL.md"),
        )
        registry.register(meta)

        provider = SkillsProvider()
        ctx = AgentContext(
            session_id="test",
            workspace=Path("."),
            project_root=Path("."),
            model="test",
            system_prompt="test",
            skill_registry=registry,
        )
        result = provider.provide(ctx)
        assert result is not None
        assert "技能系统" in result
        assert "mandatory" in result
        assert "hello" in result
        assert "read_file" in result
        assert "SKILL.md" in result

    def test_provide_empty_registry(self):
        from dotclaw.agent.prompt.providers import SkillsProvider
        from dotclaw.agent.context import AgentContext
        from dotclaw.skills.registry import SkillRegistry
        from pathlib import Path

        provider = SkillsProvider()
        ctx = AgentContext(
            session_id="test",
            workspace=Path("."),
            project_root=Path("."),
            model="test",
            system_prompt="test",
            skill_registry=SkillRegistry(),
        )
        result = provider.provide(ctx)
        assert result is None


# ============================================================
# 场景 6: AgentContext
# ============================================================

class TestAgentContextSkill:
    """AgentContext skill_registry 字段测试"""

    def test_skill_registry_field(self):
        from dotclaw.agent.context import AgentContext
        from dotclaw.skills.registry import SkillRegistry
        from pathlib import Path

        registry = SkillRegistry()
        ctx = AgentContext(
            session_id="test",
            workspace=Path("."),
            project_root=Path("."),
            model="test",
            system_prompt="test",
            skill_registry=registry,
        )
        assert ctx.skill_registry is registry

    def test_skill_registry_default_none(self):
        from dotclaw.agent.context import AgentContext
        from pathlib import Path

        ctx = AgentContext(
            session_id="test",
            workspace=Path("."),
            project_root=Path("."),
            model="test",
            system_prompt="test",
        )
        assert ctx.skill_registry is None


# ============================================================
# 场景 7: 回归测试
# ============================================================

class TestRegression:
    """Phase 7 不应影响 Phase 1-6 功能"""

    def test_imports_dont_break(self):
        from dotclaw.tools.base import ToolDefinition, ToolResult, ToolSource
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.executor import ToolExecutor
        from dotclaw.skills import SkillMeta, SkillLifecycle, SkillScanner, SkillRegistry
        from dotclaw.mcp import McpClient, MCPToolProvider

        assert ToolSource.MCP.value == "mcp"
        assert SkillLifecycle.PERSISTENT.value == "persistent"

    def test_skills_config_exists(self):
        from dotclaw.config.settings import SkillsConfig
        cfg = SkillsConfig()
        assert hasattr(cfg, "enabled")
        assert hasattr(cfg, "skip_prefix")
