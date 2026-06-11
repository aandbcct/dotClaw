# Phase 7 详细开发文档：Skill 系统完善

> 创建时间：2026-06-05
> 状态：已完成 ✅（2026-06-05）
> 依赖：Phase 1-6 已完成
> 变更日志：[docs/phase7/phase7-record.md](phase7-record.md)
> 数据结构设计：[docs/phase7/skill-dataclass-design.md](skill-dataclass-design.md)
> 审计报告：[docs/phase7/phase7-roadmap-review.md](phase7-roadmap-review.md)
> 协议版本：dotClaw Skill 规范 v1.0

---

## 一、开发目的

Phase 7 完善 dotClaw 的 Skill 系统，实现从"骨架占位"到"可用链路"的升级。Agent 启动时自动扫描 Skill 目录，将 Skill 描述注入 system prompt，LLM 根据用户需求自主选择并读取 SKILL.md，按其中的指令执行。

**核心目标**：
1. **扫描注册**——递归扫描 skills 目录，解析 SKILL.md frontmatter，构建 SkillMeta 注册表
2. **描述注入**——SkillsProvider 将技能系统提示词 + Skill 列表注入 system prompt
3. **LLM 自主激活**——LLM 判断需求匹配 → 调用 read_file 读取 SKILL.md body → 按指令执行
4. **渐进式披露**——L1 描述常驻 → L2 body 按需 read → L3 references/scripts 按 LLM 判断 read/exec
5. **配置扩展**——SkillsConfig 支持多目录、启停、跳过前缀
6. **CLI 可视**——`/skills` 命令查看已加载 Skill

**设计原则**：
- 复用现有 read_file 工具，不加新工具
- SkillsProvider 保持 DataProvider 接口，不改 PromptBuilder
- LLM 驱动激活，框架不做匹配/路由
- frontmatter 全量解析，为后续扩展预留

---

## 二、架构总览

```
启动时
═══════════════════════════════════════════════════════════════

config.yaml (skills section)
        │
        ▼
SkillsConfig (directory, enabled, skip_prefix)
        │
        ▼
SkillScanner.scan(directories, skip_prefix)
        │
        ├── 递归扫描 skills 目录（跳过 _ 前缀）
        ├── 找到所有 SKILL.md
        ├── yaml.safe_load() 解析 frontmatter
        ├── 扫描 scripts/ 和 references/ 子目录
        │
        ▼
SkillMeta (全量 frontmatter 字段 + 文件系统字段)
        │
        ▼
SkillRegistry.register(meta) × N
        │
        ▼
存入 AgentContext.skill_registry


每次请求
═══════════════════════════════════════════════════════════════

AgentLoop._build_context()
        │
        ▼
PromptBuilder.build(context)
        │
        ▼
SkillsProvider.provide(context)
        │
        ├── 读取 context.skill_registry
        ├── 生成技能系统提示词（mandatory 说明）
        ├── 生成 Skill 列表（name + description 截断20字 + 绝对路径 location）
        │
        ▼
注入 system prompt


LLM 交互
═══════════════════════════════════════════════════════════════

LLM 看到 Skill 列表
        │
        │  判断用户需求与某个 Skill 描述匹配
        ▼
LLM 调用 read_file(path="D:\...\xbrowser\SKILL.md")
        │
        ▼
read_file 返回完整文件内容（含 frontmatter）
        │
        ▼
LLM 从 body 中获取：
  - 使用方式 / 触发条件
  - scripts/ 下的脚本路径 → 需要时调 exec
  - references/ 下的文档路径 → 需要时调 read_file
        │
        ▼
LLM 按 body 指令执行后续操作
```

---

## 三、模块层级与依赖关系

```
                    config/settings.py
                    (SkillsConfig 扩展)
                           │
                           ▼
                    skills/scanner.py ────── NEW
                    (SkillScanner)
                           │
                           ▼
                    skills/models.py ─────── NEW
                    (SkillMeta, SkillLifecycle 枚举)
                           │
                           ▼
                    skills/registry.py ───── NEW
                    (SkillRegistry)
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
    agent/context.py   agent/prompt/    main.py
    (skill_registry)   providers.py    (启动编排)
                       (SkillsProvider)
```

**依赖方向**（单向，无循环）：
```
settings → models → scanner → registry → providers → context → main
```

**与现有模块的关系**：
- `skills/loader.py`：**废弃**，由 scanner.py + registry.py 替代
- `agent/prompt/providers.py`：修改 `SkillsProvider.provide()` 实现
- `agent/context.py`：新增 `skill_registry` 字段
- `config/settings.py`：扩展 `SkillsConfig`
- `main.py`：启动时创建 SkillScanner → SkillRegistry → 注入 AgentContext

---

## 四、各模块详细设计

### 4.1 SkillsConfig 扩展

**文件**：`config/settings.py`

**现状**：
```python
@dataclass
class SkillsConfig:
    directory: str = "./skills"
```

**Phase 7 目标**：
```python
@dataclass
class SkillsConfig:
    directory: str | list[str] = "./skills"   # 支持多目录
    enabled: bool = True                       # 是否启用 Skill 系统
    skip_prefix: str = "_"                     # 跳过此前缀开头的目录
```

**消费点**：
- `enabled`：**唯一权威源**，控制 Skill 系统启停。main.py 启动时判断是否初始化 Skill 系统

> ⚠️ **关于 `ToolsConfig.skill_enabled`**：P5 预留了 `config.tools.skill_enabled` 字段，
> P7 起由 `SkillsConfig.enabled` 替代。`ToolsConfig.skill_enabled` 仅保留向后兼容，不消费。
> 两个开关不同步时，以 `SkillsConfig.enabled` 为准。
- `directory`：SkillScanner 扫描的根路径列表，统一转 list 处理
- `skip_prefix`：SkillScanner 扫描时跳过匹配的子目录

**config.yaml 新格式**（向后兼容）：
```yaml
skills:
  directory: ./skills
  enabled: true
  skip_prefix: "_"

# 多目录示例
skills:
  directory:
    - ./skills
    - ./extra-skills
  enabled: true
  skip_prefix: "_"
```

**_raw_to_config() 修改**：
```python
skills_raw = raw.get("skills", {})

# directory 支持字符串或列表
dir_raw = skills_raw.get("directory", "./skills")
if isinstance(dir_raw, list):
    directory = dir_raw
else:
    directory = str(dir_raw)

skills = SkillsConfig(
    directory=directory,
    enabled=skills_raw.get("enabled", True),
    skip_prefix=skills_raw.get("skip_prefix", "_"),
)
```

---

### 4.2 SkillMeta 数据模型

**文件**：`skills/models.py`（新建）

```python
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
    保留枚举值的原因：frontmatter 可能包含 lifecycle: one-shot，
    如果不定义此枚举值，_parse_lifecycle() 降级逻辑无法正确解析。
    """
    PERSISTENT = "persistent"   # 持久：触发后一直活跃（P7 唯一消费的值）
    ONE_SHOT = "one-shot"       # 一次性：完成后自动卸载（预留，P7 不消费）
    EPHEMERAL = "ephemeral"     # 临时：每次请求重新判定（预留，P7 不消费）


@dataclass(frozen=True)
class SkillMeta:
    """
    Skill 完整元数据，始终常驻内存。

    = SKILL.md frontmatter 全字段
    + 文件系统扫描信息

    这是 SkillRegistry 的索引单位。
    占内存极小（~500 B），所有 Skill 的 Meta 始终在内存中。
    """
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
```

**字段说明**：

| 字段 | 来源 | 必有 | 说明 |
|------|------|------|------|
| `name` | frontmatter | ✅ | Skill 标识名 |
| `description` | frontmatter | ✅ | 触发描述（注入 Skill 列表） |
| `keywords` | frontmatter | ❌ | 触发关键词列表 |
| `lifecycle` | frontmatter | ❌ | 生命周期模式，默认 PERSISTENT |
| `deactivate_on` | frontmatter | ❌ | 自动卸载条件 |
| `always_load` | frontmatter `metadata.openclaw.always` | ❌ | 强制始终启用（单用户下无差别） |
| `emoji` | frontmatter `metadata.openclaw.emoji` | ❌ | 显示图标 |
| `homepage` | frontmatter | ❌ | 主页链接 |
| `author` | frontmatter | ❌ | 作者 |
| `metadata` | frontmatter `metadata` | ❌ | 扩展元数据 |
| `extra` | frontmatter 未知字段 | ❌ | 兜底字段 |
| `skill_dir` | 文件系统 | ✅ | Skill 根目录 |
| `skill_md_path` | 文件系统 | ✅ | SKILL.md 完整路径 |
| `has_scripts` | 文件系统 | ✅ | scripts/ 目录是否存在 |
| `has_references` | 文件系统 | ✅ | references/ 目录是否存在 |
| `script_paths` | 文件系统 | ✅ | scripts/ 下的相对路径清单 |
| `reference_paths` | 文件系统 | ✅ | references/ 下的相对路径清单 |

**无效值处理**：frontmatter 中的无效枚举值（如 `lifecycle: unknown`）静默降级为默认值，记录 warning 日志。

---

### 4.3 SkillScanner

**文件**：`skills/scanner.py`（新建）

**职责**：递归扫描 skills 目录，解析 SKILL.md，构建 SkillMeta 列表。

```python
"""Skill 扫描器 — Phase 7"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .models import SkillMeta, SkillLifecycle

logger = logging.getLogger("dotclaw.skills.scanner")


class SkillScanner:
    """
    递归扫描 skills 目录，构建 SkillMeta 列表。

    规则：
    - 递归扫描：找所有 SKILL.md，不限层级
    - 跳过 _ 前缀的目录
    - frontmatter 用 yaml.safe_load() 解析
    - 无效枚举值降级为默认值
    - 解析失败的 Skill 记录 warning 跳过，不中断整体扫描
    """

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
                    if not entry.is_dir():
                        continue
                    if entry.name.startswith(self._skip_prefix):
                        continue
                    skill_md = entry / "SKILL.md"
                    if skill_md.exists():
                        results.append(skill_md)
                    # 继续递归（SKILL.md 可能在嵌套目录中）
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

        # 提取 frontmatter
        fm = self._parse_frontmatter(content)
        if fm is None:
            logger.warning(f"SKILL.md 无有效 frontmatter: {skill_md}")
            return None

        if not fm.get("name"):
            logger.warning(f"SKILL.md 缺少 name 字段: {skill_md}")
            return None

        # 扫描 scripts/ 和 references/
        script_paths = self._scan_subdir(skill_dir, "scripts")
        reference_paths = self._scan_subdir(skill_dir, "references")

        # 提取 metadata.openclaw 子字段
        metadata = fm.get("metadata", {})
        openclaw = metadata.get("openclaw", {}) if isinstance(metadata, dict) else {}

        # 解析 lifecycle（无效值降级为 PERSISTENT）
        lifecycle = self._parse_lifecycle(fm.get("lifecycle", "persistent"))

        # 解析 deactivate_on
        deactivate_raw = fm.get("deactivate_on", [])
        deactivate_on = tuple(deactivate_raw) if isinstance(deactivate_raw, list) else ()

        # 解析 keywords
        keywords_raw = fm.get("keywords", [])
        keywords = tuple(keywords_raw) if isinstance(keywords_raw, list) else ()

        # 收集已知字段之外的 extra
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
        # 规范化换行符：Windows 下文件可能使用 \r\n
        content = content.replace('\r\n', '\n')

        import re
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
        if not subdir.is_dir():
            return []

        paths: list[str] = []
        for f in subdir.rglob("*"):
            if f.is_file():
                paths.append(str(f.relative_to(skill_dir)))
        return sorted(paths)
```

**关键设计决策**：
- `_find_skill_files()` 递归遍历，发现 SKILL.md 后不停止递归（支持嵌套 Skill）
- `yaml.safe_load()` 替代手工逐行解析，支持嵌套字段和多行值
- 无效 lifecycle 值降级为 PERSISTENT，不跳过整个 Skill
- `_scan_subdir()` 用 `rglob("*")` 递归扫描 scripts/ 和 references/ 下的所有文件

---

### 4.4 SkillRegistry

**文件**：`skills/registry.py`（新建）

**职责**：SkillMeta 的索引容器，提供查询和描述块生成。

```python
"""Skill 注册表 — Phase 7"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import SkillMeta

logger = logging.getLogger("dotclaw.skills.registry")


class SkillRegistry:
    """
    Skill 元数据注册表。

    职责：
    - register / get / list_all：基本 CRUD
    - get_descriptions_block：生成注入 system prompt 的描述文本
    """

    def __init__(self):
        self._index: dict[str, "SkillMeta"] = {}

    def register(self, meta: "SkillMeta") -> None:
        """注册 Skill。同名后注册覆盖前注册（静默覆盖）。"""
        self._index[meta.name] = meta

    def get(self, name: str) -> "SkillMeta | None":
        """按名称获取 Skill 元数据。"""
        return self._index.get(name)

    def list_all(self) -> list["SkillMeta"]:
        """返回所有已注册的 Skill 元数据。"""
        return list(self._index.values())

    def get_descriptions_block(self, max_desc_len: int = 20) -> str:
        """
        生成注入 system prompt 的 Skill 描述列表。

        格式：
        - **name**: description 截断 `D:\...\SKILL.md`
        """
        if not self._index:
            return ""

        lines = []
        for meta in sorted(self._index.values(), key=lambda m: m.name):
            desc = meta.description
            # 取第一行作为摘要
            first_line = desc.split("\n")[0].strip()
            if len(first_line) > max_desc_len:
                first_line = first_line[:max_desc_len] + "..."
            location = str(meta.skill_md_path)
            lines.append(f"- **{meta.name}**: {first_line} `{location}`")

        return "\n".join(lines)
```

---

### 4.5 SkillsProvider 实现

**文件**：`agent/prompt/providers.py`（修改现有）

**现状**：
```python
class SkillsProvider(DataProvider):
    @property
    def section_name(self) -> str:
        return "skills"

    def provide(self, context: "AgentContext") -> str | None:
        return None  # P7 实现
```

**Phase 7 目标**：
```python
class SkillsProvider(DataProvider):
    """技能描述（Phase 7 实现）— 从 context.skill_registry 读取"""

    @property
    def section_name(self) -> str:
        return "skills"

    def provide(self, context: "AgentContext") -> str | None:
        registry = context.skill_registry
        if not registry:
            return None

        descriptions = registry.get_descriptions_block(max_desc_len=20)
        if not descriptions:
            return None

        return (
            "## 技能系统（mandatory）\n\n"
            "如果有技能的描述与用户需求匹配：使用 `read_file` 工具读取其路径的 SKILL.md 文件，\n"
            "然后严格遵循文件中的指令。\n\n"
            "**重要**: 技能不是工具，不能直接调用。使用技能的唯一方式是用 `read_file` 读取 SKILL.md 文件，\n"
            "然后按文件内容操作。\n\n"
            "### 可用技能\n\n"
            f"{descriptions}"
        )
```

**注入效果示例**：
```
## 技能系统（mandatory）

如果有技能的描述与用户需求匹配：使用 `read_file` 工具读取其路径的 SKILL.md 文件，
然后严格遵循文件中的指令。

**重要**: 技能不是工具，不能直接调用。使用技能的唯一方式是用 `read_file` 读取 SKILL.md 文件，
然后按文件内容操作。

### 可用技能

- **hello**: 示例技能：演示 Skill 系... `D:\dev\dotclaw\skills\_example\SKILL.md`
- **xbrowser**: EXCLUSIVE browser au... `D:\dev\dotclaw\skills\xbrowser\SKILL.md`
```

> **前瞻**：Skill 数量超过 20 时，可考虑在 SkillsProvider 中基于用户 query 做关键词预过滤，
> 仅注入匹配的 Skill 描述，减少 system prompt token 占用。当前 P7 范围暂不处理。

---

### 4.6 AgentContext 扩展

**文件**：`agent/context.py`（修改现有）

新增字段：
```python
@dataclass(frozen=True)
class AgentContext:
    # ... 现有字段 ...

    skill_registry: "SkillRegistry | None" = None
    """P7 新增：Skill 注册表（始终有，skill_enabled=False 时为 None）"""
```

**TYPE_CHECKING 导入**：
```python
if TYPE_CHECKING:
    from ..llm.base import ToolDefinition
    from ..channel.base import Channel
    from ..skills.registry import SkillRegistry  # P7 新增
```

---

### 4.7 main.py 启动编排

**文件**：`main.py`（修改现有）

在 Phase 5 工具层初始化和 Phase 6 MCP 初始化之间，添加 Skill 系统初始化：

```python
# ---- Phase 7 新增：Skill 系统初始化 ----
skill_registry = None
if config.skills.enabled:
    from dotclaw.skills.scanner import SkillScanner
    from dotclaw.skills.registry import SkillRegistry

    # directory 支持字符串或列表
    skill_dirs = config.skills.directory
    if isinstance(skill_dirs, str):
        skill_dirs = [skill_dirs]

    # 相对路径基于 project_root 解析
    skill_paths = [
        str(project_root / d) if not Path(d).is_absolute() else d
        for d in skill_dirs
    ]

    scanner = SkillScanner(skill_paths, skip_prefix=config.skills.skip_prefix)
    metas = scanner.scan()

    skill_registry = SkillRegistry()
    for meta in metas:
        skill_registry.register(meta)

    channel.print_info(f"  已加载 {len(metas)} 个 Skill")
# ---- Phase 7 Skill 初始化结束 ----
```

AgentLoop 创建后，在 `_build_context()` 调用时传入 `skill_registry`。

---

### 4.8 AgentLoop._build_context() 修改

**文件**：`agent/loop.py`（修改现有）

在 `_build_context()` 中新增 `skill_registry` 字段：

```python
return Ctx(
    # ... 现有字段 ...
    skill_registry=skill_registry,  # P7 新增
)
```

需要将 `skill_registry` 传递到 AgentLoop：

**方案**：将 `skill_registry` 作为 AgentLoop 的初始化参数。

```python
class AgentLoop:
    def __init__(
        self,
        # ... 现有参数 ...
        skill_registry: "SkillRegistry | None" = None,  # P7 新增
    ):
        # ...
        self._skill_registry = skill_registry
```

在 `_build_context()` 中：
```python
return Ctx(
    # ... 现有字段 ...
    skill_registry=self._skill_registry,
)
```

---

### 4.9 PromptBuilder 注册 SkillsProvider

**文件**：`main.py`（修改现有）

```python
# P7：SkillsProvider 需要 skill_registry，但 DataProvider 接口通过 context 传递
# 所以只需注册到 PromptBuilder 即可
prompt_builder = PromptBuilder([
    RoleProvider(),
    RulesProvider(),
    ToolsProvider(),
    MemoryProvider(),
    SkillsProvider(),     # ← P7 激活
])
```

SkillsProvider 从 `context.skill_registry` 读取，不需要构造参数。

---

### 4.10 /skills CLI 命令

**文件**：`main.py`（修改现有）

新增 `/skills` 命令处理：

```python
elif cmd == "/skills":
    _cmd_skills(channel, skill_registry)
```

```python
def _cmd_skills(channel, skill_registry):
    """列出所有已加载的 Skill"""
    if not skill_registry:
        channel.print_info("Skill 系统未启用")
        return

    metas = skill_registry.list_all()
    if not metas:
        channel.print_info("(没有加载任何 Skill)")
        return

    channel.print_info(f"已加载 Skill ({len(metas)} 个):")
    for meta in sorted(metas, key=lambda m: m.name):
        desc = meta.description.split("\n")[0].strip()
        if len(desc) > 20:
            desc = desc[:20] + "..."
        channel.print_info(f"  {meta.name}: {desc}")
```

`/help` 输出新增：
```
  /skills          列出已加载技能
```

---

### 4.11 skills/loader.py 废弃

**文件**：`skills/loader.py`（删除或标记废弃）

现有 SkillLoader 和 Skill 类由 scanner.py + models.py + registry.py 完全替代。

处理方式：
- 删除 `skills/loader.py`
- 从 `skills/__init__.py` 移除对 SkillLoader 的导出

---

### 4.12 skills/__init__.py 更新

**文件**：`skills/__init__.py`（修改现有）

```python
"""Skill 模块（Phase 7）"""

from .models import SkillMeta, SkillLifecycle
from .scanner import SkillScanner
from .registry import SkillRegistry

__all__ = [
    "SkillMeta",
    "SkillLifecycle",
    "SkillScanner",
    "SkillRegistry",
]
```

---

## 五、数据流转图

```
┌─────────────────────────────────────────────────────────┐
│ 启动时                                                   │
│                                                          │
│  config.yaml                                             │
│      │                                                   │
│      ▼                                                   │
│  SkillsConfig                                            │
│  (directory, enabled, skip_prefix)                       │
│      │                                                   │
│      ▼                                                   │
│  SkillScanner.scan()                                     │
│      │                                                   │
│      ├── 遍历 directories                                │
│      ├── 递归查找 SKILL.md（跳过 _ 前缀目录）            │
│      ├── yaml.safe_load() 解析 frontmatter              │
│      ├── 扫描 scripts/ references/ 子目录               │
│      │                                                   │
│      ▼                                                   │
│  list[SkillMeta]                                         │
│      │                                                   │
│      ▼                                                   │
│  SkillRegistry.register() × N                            │
│      │                                                   │
│      ▼                                                   │
│  AgentLoop(skill_registry=registry)                      │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ 每次请求                                                 │
│                                                          │
│  AgentLoop.run()                                         │
│      │                                                   │
│      ▼                                                   │
│  _build_context()                                        │
│      │                                                   │
│      ▼                                                   │
│  AgentContext(skill_registry=registry)                   │
│      │                                                   │
│      ▼                                                   │
│  PromptBuilder.build(context)                            │
│      │                                                   │
│      ▼                                                   │
│  SkillsProvider.provide(context)                         │
│      │                                                   │
│      ├── 技能系统提示词（mandatory 说明）                │
│      ├── Skill 列表（name + desc 截断 + location）       │
│      │                                                   │
│      ▼                                                   │
│  system prompt 中包含 Skill 描述                         │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ LLM 交互                                                │
│                                                          │
│  LLM 看到 Skill 列表                                    │
│      │                                                   │
│      │  用户需求与某个 Skill 描述匹配                    │
│      ▼                                                   │
│  LLM 调用 read_file(path="...\\xbrowser\\SKILL.md")     │
│      │                                                   │
│      ▼                                                   │
│  read_file 返回 SKILL.md 全文（含 frontmatter + body）  │
│      │                                                   │
│      ▼                                                   │
│  LLM 从 body 中获取使用指引                              │
│      │                                                   │
│      ├── scripts/ 脚本路径 → exec 执行                  │
│      ├── references/ 文档路径 → read_file 读取           │
│      └── 直接按 body 指令操作                            │
└─────────────────────────────────────────────────────────┘
```

---

## 六、内存占用估算

| 数据 | 加载时机 | 单个占用 | 10 个 Skill 总计 |
|------|---------|---------|-----------------|
| SkillMeta | 启动时 | ~500 B | ~5 KB |
| SkillRegistry._index | 启动时 | ~100 B/key | ~1 KB |
| 描述块（prompt 文本） | 每次请求 | ~50 B/skill | ~500 B |

**总计**：10 个 Skill ≈ **6.5 KB**，完全可以忽略。

---

## 七、文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `skills/models.py` | 新建 | SkillMeta + SkillLifecycle |
| `skills/scanner.py` | 新建 | SkillScanner |
| `skills/registry.py` | 新建 | SkillRegistry |
| `skills/__init__.py` | 修改 | 更新导出 |
| `skills/loader.py` | 删除 | 由 scanner + registry 替代 |
| `config/settings.py` | 修改 | SkillsConfig 扩展 + _raw_to_config 更新 |
| `agent/context.py` | 修改 | 新增 skill_registry 字段 |
| `agent/prompt/providers.py` | 修改 | SkillsProvider.provide() 实现 |
| `agent/loop.py` | 修改 | AgentLoop 接收 skill_registry，_build_context 传入 |
| `main.py` | 修改 | Skill 初始化 + /skills 命令 + SkillsProvider 注册 |

---

## 八、开发顺序

按依赖关系从底向上，分 4 个步骤：

### Step 1：数据层
1. 新建 `skills/models.py`（SkillMeta + SkillLifecycle）
2. 扩展 `config/settings.py`（SkillsConfig + _raw_to_config）

### Step 2：扫描 + 注册
3. 新建 `skills/scanner.py`（SkillScanner）
4. 新建 `skills/registry.py`（SkillRegistry）
5. 更新 `skills/__init__.py`
6. 删除 `skills/loader.py`

### Step 3：集成层
7. 修改 `agent/context.py`（新增 skill_registry）
8. 修改 `agent/prompt/providers.py`（SkillsProvider 实现）
9. 修改 `agent/loop.py`（skill_registry 传递）

### Step 4：启动 + CLI
10. 修改 `main.py`（Skill 初始化 + SkillsProvider 注册 + /skills 命令）
11. 端到端测试

---

## 九、测试要点

### 9.1 单元测试

| 测试项 | 验证点 |
|--------|--------|
| SkillScanner 正常扫描 | 解析 SKILL.md frontmatter，构建正确的 SkillMeta |
| SkillScanner 跳过 _ 前缀 | `_example` 目录被跳过 |
| SkillScanner 递归扫描 | 嵌套目录中的 SKILL.md 被发现 |
| SkillScanner 无效 frontmatter | 缺少 name 跳过，无效 lifecycle 降级 |
| SkillScanner 多目录 | 两个目录下的 Skill 都被扫描 |
| SkillRegistry CRUD | register / get / list_all 正常 |
| SkillRegistry 重复名称 | 后注册覆盖前注册 |
| SkillRegistry.get_descriptions_block | 格式正确，description 截断 20 字 |
| SkillsConfig 解析 | 单字符串/列表 directory 均可 |
| SkillsProvider.provide | 返回正确格式的 prompt 段 |

### 9.2 集成测试

| 测试项 | 验证点 |
|--------|--------|
| 启动 → Skill 加载 | main.py 启动后 Skill 数量正确 |
| system prompt 包含 Skill 描述 | PromptBuilder 输出包含技能系统提示词 + 列表 |
| skill_enabled=false | Skill 系统不初始化，SkillRegistry 为 None |
| /skills 命令 | 输出正确的 Skill 列表 |
| LLM → read_file → 执行 | LLM 读取 SKILL.md 后按指令调用 exec |

### 9.3 边界测试

| 测试项 | 验证点 |
|--------|--------|
| skills 目录不存在 | 不报错，返回空列表 |
| SKILL.md 为空文件 | 跳过，记录 warning |
| SKILL.md 只有 body 无 frontmatter | 跳过，记录 warning |
| SKILL.md 使用 \r\n 换行符 | 规范化后正确解析 |
| frontmatter 多行 description | 正确解析 |
| metadata.openclaw.always: true | always_load=True |
| 嵌套 Skill 目录 | 递归扫描正常 |

---

## 十、不做的事项（明确排除）

| 功能 | 原因 |
|------|------|
| SkillFrontmatter / SkillPackageMeta | 字段已合并到 SkillMeta |
| SkillBody / SkillScript / SkillReference | 框架不主动加载，LLM 通过 read_file/exec 访问 |
| LoadedSkill | 当前无框架主动管理 Skill 生命周期的需求 |
| SkillCache | body 不通过框架缓存，LLM 直接 read_file |
| read_skill_body 工具 | 复用现有 read_file，不加新工具 |
| SkillToolProvider | Skill 不注册为工具，通过 prompt 注入 |
| SkillMatcher | LLM 自主判断激活，框架不做匹配 |
| SkillLifecycle.ONE_SHOT/EPHEMERAL 消费 | P7 仅定义不消费，生命周期管理留待后续 Phase。保留枚举值是因为 frontmatter 可能包含这些值，降级解析需要目标枚举 |
| ToolsConfig.skill_enabled 消费 | P5 预留字段，P7 起由 SkillsConfig.enabled 替代，仅保留向后兼容不消费 |
| 热加载 | 启动时一次性扫描，运行时不变 |
| 创建向导 | 超出 Phase 7 范围 |
| SKILL.md body 去 frontmatter | read_file 返回完整内容，LLM 自然忽略 YAML 头部 |

---

## 十一、SKILL.md 规范参考

Phase 7 支持的 SKILL.md frontmatter 完整字段：

```yaml
---
name: skill-name                   # 必填，Skill 标识名
description: |                     # 必填，触发描述
  描述文本...
keywords:                          # 可选，触发关键词列表
  - keyword1
  - keyword2
lifecycle: persistent              # 可选，persistent | one-shot | ephemeral
deactivate_on:                     # 可选，自动卸载条件
  - condition1
homepage: https://example.com      # 可选，主页链接
author: Author Name                # 可选，作者
metadata:                          # 可选，扩展元数据
  openclaw:
    always: true                   # 可选，强制始终启用
    emoji: "🌐"                    # 可选，显示图标
  custom_key: custom_value         # 可选，自定义字段
---

# Skill 标题

Body 内容（Markdown）...

## 脚本

```bash
python scripts/example.py
```

## 参考

详见 references/ 下的文档。
```

**Skill 目录结构**：

```
skill-name/
├── SKILL.md          # 必填
├── scripts/          # 可选
│   └── example.py
├── references/       # 可选
│   └── guide.md
└── _meta.json        # 可选（Phase 7 不消费）
```
