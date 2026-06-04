# Skill 模块 Dataclass 设计文档

> 版本：v1.0 | 更新日期：2026-06-04
> 
> 本文档定义 Agent 系统中 Skill 模块的完整 dataclass 体系，涵盖从包元数据到运行时加载的全链路数据结构。

---

## 一、设计原则

| 原则 | 说明 |
|------|------|
| **元数据与内容分离** | 元数据轻量常驻内存，内容按需加载 |
| **渐进式披露** | L1 描述 → L2 body → L3 references，逐层按需加载 |
| **不可变设计** | 所有 dataclass 使用 `frozen=True`，修改通过 `replace()` 返回新对象 |
| **从实际 SKILL.md 出发** | 字段设计来源于现有 Skill 的 frontmatter 实际字段，不凭空设计 |
| **路径先行、内容后补** | 脚本和引用文档只存路径清单，内容在 LLM 需要 read/exec 时才加载 |

---

## 二、Skill 包结构参考

实际 Skill 包存在三种复杂度：

### 极简型（纯知识）

```
qclaw-rules/
└── SKILL.md
```

### 标准型（知识 + 脚本）

```
qclaw-text-file/
├── SKILL.md
└── scripts/
    └── write_file.py
```

### 完整型（知识 + 脚本 + 引用 + 元数据）

```
xbrowser/
├── _meta.json
├── SKILL.md
├── scripts/
│   ├── package.json
│   └── xb.cjs
└── references/
    ├── authentication.md
    ├── recipes.md
    └── xb-browser-commands.md
```

---

## 三、层级模型总览

```
┌─────────────────────────────────────────────────────────┐
│ L0: SkillPackageMeta  (_meta.json)                      │
│     slug, version                                        │
│     占用 ~50 B，启动时加载                               │
├─────────────────────────────────────────────────────────┤
│ L1: SkillMeta  (frontmatter + 文件系统扫描)             │
│     name, description, keywords, lifecycle,              │
│     skill_dir, script_paths, reference_paths             │
│     占用 ~500 B，启动时加载，始终常驻内存                │
├─────────────────────────────────────────────────────────┤
│ L2: SkillBody  (SKILL.md body)                          │
│     content (Markdown 正文)                              │
│     占用 ~2-5 KB，触发后加载，带缓存                     │
├─────────────────────────────────────────────────────────┤
│ L3: SkillReference  (references/ 下的文档)              │
│     relative_path, absolute_path                         │
│     内容按需填充，LLM 执行中 read 时才加载               │
├─────────────────────────────────────────────────────────┤
│ L1.5: SkillScript  (scripts/ 下的脚本)                  │
│     relative_path, absolute_path, language               │
│     不注册为工具，只记录路径供 exec 调用                  │
├─────────────────────────────────────────────────────────┤
│ 聚合: LoadedSkill                                       │
│     = SkillMeta + SkillBody + Scripts + References       │
│     占用 ~3-6 KB，触发后创建，存入 AgentContext          │
└─────────────────────────────────────────────────────────┘
```

---

## 四、Dataclass 完整定义

### 4.1 L0: SkillPackageMeta

```python
@dataclass(frozen=True)
class SkillPackageMeta:
    """
    包管理元数据，来自 _meta.json。
    
    不是每个 Skill 都有此文件，缺失时用默认值。
    
    示例 _meta.json:
    {
        "slug": "xbrowser",
        "version": "1.2.0"
    }
    """
    slug: str = ""           # 包标识符（与目录名一致）
    version: str = "0.0.0"   # 语义化版本号
```

### 4.2 SkillFrontmatter（中间态，不直接存入 Registry）

```python
class SkillLifecycle(StrEnum):
    """Skill 生命周期模式"""
    PERSISTENT = "persistent"   # 持久：触发后一直活跃直到会话结束
    ONE_SHOT = "one-shot"       # 一次性：完成特定阶段后自动卸载
    EPHEMERAL = "ephemeral"     # 临时：每次请求重新判定是否加载


@dataclass(frozen=True)
class SkillFrontmatter:
    """
    SKILL.md 的 YAML frontmatter 解析结果。
    
    实际 frontmatter 字段统计（从现有 Skill 提取）：
    
    ┌──────────────────┬────────┬──────────────────────────────────────┐
    │ 字段              │ 必有   │ 说明                                  │
    ├──────────────────┼────────┼──────────────────────────────────────┤
    │ name             │ ✅     │ Skill 标识名                          │
    │ description      │ ✅     │ 触发描述（注入描述块）                 │
    │ keywords         │ ❌     │ 触发关键词列表（xbrowser 有）          │
    │ lifecycle        │ ❌     │ 生命周期模式（another_them 有）        │
    │ deactivate_on    │ ❌     │ 自动卸载条件                          │
    │ metadata         │ ❌     │ 扩展字段                              │
    │   .openclaw.always │ ❌   │ 强制始终加载（qclaw-rules 有）        │
    │   .openclaw.emoji │ ❌   │ 显示图标                              │
    └──────────────────┴────────┴──────────────────────────────────────┘
    
    示例 frontmatter:
    
    ---
    name: xbrowser
    description: |
      EXCLUSIVE browser automation — REPLACES built-in ...
    keywords:
      - "open webpage"
      - "browser"
      - "screenshot"
    metadata:
      openclaw:
        emoji: "🌐"
    ---
    
    ---
    name: another_them
    description: |
      另一个TA：输入人名/主题/模糊需求...
    lifecycle: one-shot
    deactivate_on:
      - phase-complete: 5
      - user-explicit-exit: true
    ---
    
    ---
    name: qclaw-rules
    description: |
      [SYSTEM RULES - MANDATORY - ALWAYS LOAD - DO NOT SKIP]
      ...
    metadata:
      openclaw:
        emoji: "📋"
        always: true
    ---
    """
    name: str
    description: str
    keywords: tuple[str, ...] = ()
    lifecycle: SkillLifecycle = SkillLifecycle.PERSISTENT
    deactivate_on: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)
    # 从 metadata.openclaw 提取的便捷字段
    always_load: bool = False
    emoji: str = ""
```

### 4.3 L1: SkillMeta

```python
@dataclass(frozen=True)
class SkillMeta:
    """
    L1: Skill 完整元数据。
    
    = SkillFrontmatter（来自 SKILL.md frontmatter）
    + SkillPackageMeta（来自 _meta.json）
    + 文件系统信息（扫描时填充）
    
    这是 SkillRegistry 的索引单位。
    占内存极小（~500 B），所有 Skill 的 Meta 始终在内存中。
    用于触发判断和描述块生成。
    """
    # ── 来自 frontmatter ──
    name: str
    description: str
    keywords: tuple[str, ...]
    lifecycle: SkillLifecycle
    deactivate_on: tuple[str, ...]
    always_load: bool
    emoji: str
    extra: dict[str, Any]

    # ── 来自 _meta.json ──
    slug: str
    version: str

    # ── 来自文件系统扫描 ──
    skill_dir: Path                      # Skill 根目录
    skill_md_path: Path                  # SKILL.md 完整路径
    has_scripts: bool                    # scripts/ 目录是否存在
    has_references: bool                 # references/ 目录是否存在
    script_paths: tuple[str, ...] = ()   # scripts/ 下的相对路径清单
    reference_paths: tuple[str, ...] = () # references/ 下的相对路径清单
```

### 4.4 L2: SkillBody

```python
@dataclass(frozen=True)
class SkillBody:
    """
    L2: SKILL.md 的 body 内容（去掉 frontmatter 后的 Markdown）。
    
    触发后才加载，注入 system prompt。
    带缓存：首次读取后缓存 body 文本，文件变更时失效。
    
    占用 ~2-5 KB（取决于 SKILL.md 长度）。
    """
    skill_name: str
    content: str               # Markdown 正文（frontmatter 之后的全部内容）
    token_count: int = 0       # 预估 token 数（用于 prompt budget 控制）
    source_path: Path = field(default_factory=Path)
```

### 4.5 L1.5: SkillScript

```python
class ScriptLanguage(StrEnum):
    """脚本语言类型"""
    PYTHON = "python"
    SHELL = "shell"
    NODE = "node"


@dataclass(frozen=True)
class SkillScript:
    """
    scripts/ 下的单个脚本信息。
    
    不注册为工具，只记录元信息供 exec 调用。
    不预加载脚本内容，LLM 需要执行时通过 exec 调用 absolute_path。
    
    占用 ~100 B。
    """
    skill_name: str
    relative_path: str         # 如 "write_file.py" 或 "office/pack.py"
    absolute_path: Path        # 完整路径，供 exec 直接使用
    language: ScriptLanguage

    @staticmethod
    def detect_language(path: Path) -> ScriptLanguage:
        """根据文件后缀检测语言类型"""
        suffix_map = {
            ".py": ScriptLanguage.PYTHON,
            ".sh": ScriptLanguage.SHELL,
            ".cjs": ScriptLanguage.NODE,
            ".js": ScriptLanguage.NODE,
            ".mjs": ScriptLanguage.NODE,
        }
        return suffix_map.get(path.suffix, ScriptLanguage.PYTHON)
```

### 4.6 L3: SkillReference

```python
@dataclass(frozen=True)
class SkillReference:
    """
    L3: references/ 下的单个文档信息。
    
    LLM 在执行过程中按需 read，不预加载。
    content 字段在加载前为空字符串，加载后才填充。
    
    占用 ~100 B（未加载时）/ ~1-10 KB（加载后）。
    """
    skill_name: str
    relative_path: str         # 如 "authentication.md" 或 "agent-templates/SOUL.template.md"
    absolute_path: Path        # 完整路径，供 read 工具使用
    content: str = ""          # 按需填充（加载前为空）
```

### 4.7 聚合: LoadedSkill

```python
@dataclass(frozen=True)
class LoadedSkill:
    """
    聚合对象：一个触发后完整加载的 Skill。
    
    = SkillMeta（L1，始终有）
    + SkillBody（L2，触发后有）
    + 可用脚本列表（L1.5，只存路径，不加载内容）
    + 可用引用列表（L3，只存路径，不加载内容）
    
    存入 AgentContext.active_skills，贯穿当前请求的整个生命周期。
    占用 ~3-6 KB。
    """
    meta: SkillMeta
    body: SkillBody
    scripts: tuple[SkillScript, ...] = ()
    references: tuple[SkillReference, ...] = ()

    @property
    def name(self) -> str:
        return self.meta.name

    @property
    def skill_dir(self) -> Path:
        return self.meta.skill_dir

    def get_script(self, relative_path: str) -> SkillScript | None:
        """按相对路径查找脚本"""
        for s in self.scripts:
            if s.relative_path == relative_path:
                return s
        return None

    def get_reference(self, relative_path: str) -> SkillReference | None:
        """按相对路径查找引用文档"""
        for r in self.references:
            if r.relative_path == relative_path:
                return r
        return None

    def to_prompt_block(self) -> str:
        """生成注入 system prompt 的文本块"""
        return f"## Skill: {self.name}\n{self.body.content}"
```

---

## 五、与系统其他部分的集成

### 5.1 AgentContext 中的 Skill 字段

```python
@dataclass(frozen=True)
class AgentContext:
    # ... 其他字段 ...

    skill_registry: SkillRegistry                         # L1：注册表（始终有）
    active_skills: tuple[LoadedSkill, ...] = ()           # L2+：当前活跃的 Skill

    def with_active_skills(self, skills: tuple[LoadedSkill, ...]) -> AgentContext:
        """返回更新了 active_skills 的新 context"""
        return replace(self, active_skills=skills)
```

### 5.2 PromptBuilder 中的 Skill 注入

```python
class PromptBuilder:
    def build(self, sections: list[str], context: AgentContext) -> str:
        parts = []

        for section in sections:
            if section == "skill_descriptions":
                # L1：所有 Skill 的描述块（始终注入，用于触发判断）
                parts.append(context.skill_registry.get_descriptions_block())

            elif section == "skill_bodies":
                # L2：活跃 Skill 的 body（触发后注入）
                for skill in context.active_skills:
                    parts.append(skill.to_prompt_block())

        return "\n\n".join(parts)
```

### 5.3 SkillScanner 中的构建逻辑

```python
class SkillScanner:
    def scan(self) -> list[SkillMeta]:
        results = []

        for base_path in self.skill_paths:
            for skill_dir in base_path.iterdir():
                skill_md = skill_dir / "SKILL.md"
                if not skill_md.exists():
                    continue

                # 解析 frontmatter → SkillFrontmatter
                fm = self._parse_frontmatter(skill_md)

                # 解析 _meta.json → SkillPackageMeta
                pkg = self._parse_meta_json(skill_dir)

                # 扫描文件系统
                script_paths = self._scan_scripts(skill_dir)
                ref_paths = self._scan_references(skill_dir)

                meta = SkillMeta(
                    # frontmatter 字段
                    name=fm.name,
                    description=fm.description,
                    keywords=fm.keywords,
                    lifecycle=fm.lifecycle,
                    deactivate_on=fm.deactivate_on,
                    always_load=fm.always_load,
                    emoji=fm.emoji,
                    extra=fm.extra,
                    # _meta.json 字段
                    slug=pkg.slug or fm.name,
                    version=pkg.version,
                    # 文件系统字段
                    skill_dir=skill_dir,
                    skill_md_path=skill_md,
                    has_scripts=len(script_paths) > 0,
                    has_references=len(ref_paths) > 0,
                    script_paths=tuple(script_paths),
                    reference_paths=tuple(ref_paths),
                )
                results.append(meta)

        return results
```

---

## 六、数据流转图

```
启动时
═══════════════════════════════════════════════════════════════

SKILL.md ──parse──▶ SkillFrontmatter ─┐
                                      ├─▶ SkillMeta ──▶ 存入 SkillRegistry._index
_meta.json ──parse──▶ SkillPackageMeta─┘     (常驻内存，所有 Skill)
                 │
scripts/ ──scan──▶ script_paths ─────────────┘
references/ ──scan──▶ reference_paths ───────┘


每次请求
═══════════════════════════════════════════════════════════════

SkillRegistry._index (全部 SkillMeta)
        │
        ▼
SkillMatcher.match(user_message)
        │
        ▼ 匹配到的 SkillMeta 列表
SkillLoader.load(skill_name)
        │
        ├──▶ 读 SKILL.md body ──▶ SkillBody
        ├──▶ 构建 SkillScript 列表（只建路径，不读内容）
        ├──▶ 构建 SkillReference 列表（只建路径，不读内容）
        │
        ▼
LoadedSkill ──▶ 存入 AgentContext.active_skills


LLM 执行中
═══════════════════════════════════════════════════════════════

AgentContext.active_skills[i]
        │
        │  LLM 需要 read references/authentication.md
        ▼
loaded_skill.get_reference("authentication.md")
        │
        ▼ 返回 SkillReference（path 已知，content 未加载）
read(SkillReference.absolute_path)        ← LLM 调用 read 工具

        │  LLM 需要执行 scripts/write_file.py
        ▼
loaded_skill.get_script("write_file.py")
        │
        ▼ 返回 SkillScript（path 已知）
exec("python SkillScript.absolute_path --args ...")   ← LLM 调用 exec 工具
```

---

## 七、内存占用估算

| Dataclass | 层级 | 加载时机 | 单个占用 | 10 个 Skill 总计 |
|-----------|------|---------|---------|-----------------|
| `SkillPackageMeta` | L0 | 启动时 | ~50 B | ~500 B |
| `SkillFrontmatter` | — | 启动时（中间态） | ~200 B | ~2 KB |
| `SkillMeta` | L1 | 启动时 | ~500 B | ~5 KB |
| `SkillBody` | L2 | 触发后 | ~2-5 KB | ~5-15 KB（按 3 个触发计） |
| `SkillScript` | L1.5 | 触发后 | ~100 B | ~1 KB |
| `SkillReference` | L3 | 按需 | ~100 B（未加载）| ~1 KB |
| `LoadedSkill` | 聚合 | 触发后 | ~3-6 KB | ~10-20 KB（按 3 个触发计） |

**总计**：10 个 Skill（3 个触发）≈ **25-45 KB**，完全可以忽略。

---

## 八、缓存策略

| 层级 | 缓存策略 | 失效条件 |
|------|---------|---------|
| L1 SkillMeta | 启动时加载，运行时不变 | Skill 安装/卸载时全量刷新 |
| L2 SkillBody | 首次读取后缓存 | SKILL.md 文件修改时间变化时失效 |
| L3 SkillReference | LRU 缓存（最近使用的保留） | 引用文件修改时间变化时失效 |

```python
class SkillCache:
    def __init__(self, max_body_cache: int = 20, max_ref_cache: int = 10):
        self.body_cache: dict[str, SkillBody] = {}                 # name → body
        self.ref_cache: OrderedDict[tuple[str, str], str] = OrderedDict()  # (skill, ref) → content
        self.max_body_cache = max_body_cache
        self.max_ref_cache = max_ref_cache

    def get_body(self, name: str) -> SkillBody | None:
        return self.body_cache.get(name)

    def put_body(self, body: SkillBody):
        self.body_cache[body.skill_name] = body

    def get_ref(self, skill: str, ref: str) -> str | None:
        key = (skill, ref)
        if key in self.ref_cache:
            self.ref_cache.move_to_end(key)  # LRU
            return self.ref_cache[key]
        return None

    def put_ref(self, skill: str, ref: str, content: str):
        key = (skill, ref)
        self.ref_cache[key] = content
        if len(self.ref_cache) > self.max_ref_cache:
            self.ref_cache.popitem(last=False)  # 淘汰最旧的

    def invalidate(self, skill_name: str | None = None):
        if skill_name:
            self.body_cache.pop(skill_name, None)
            self.ref_cache = OrderedDict(
                (k, v) for k, v in self.ref_cache.items() if k[0] != skill_name
            )
        else:
            self.body_cache.clear()
            self.ref_cache.clear()
```

---

## 九、完整代码文件

```python
# agent/skills/models.py

from __future__ import annotations
from dataclasses import dataclass, field, replace
from pathlib import Path
from enum import StrEnum
from typing import Any
from collections import OrderedDict


# ═══════════════════════════════════════════════════════
#  枚举类型
# ═══════════════════════════════════════════════════════

class SkillLifecycle(StrEnum):
    """Skill 生命周期模式"""
    PERSISTENT = "persistent"
    ONE_SHOT = "one-shot"
    EPHEMERAL = "ephemeral"


class ScriptLanguage(StrEnum):
    """脚本语言类型"""
    PYTHON = "python"
    SHELL = "shell"
    NODE = "node"


# ═══════════════════════════════════════════════════════
#  L0: 包元数据
# ═══════════════════════════════════════════════════════

@dataclass(frozen=True)
class SkillPackageMeta:
    """包管理元数据，来自 _meta.json"""
    slug: str = ""
    version: str = "0.0.0"


# ═══════════════════════════════════════════════════════
#  中间态: Frontmatter 解析结果
# ═══════════════════════════════════════════════════════

@dataclass(frozen=True)
class SkillFrontmatter:
    """SKILL.md 的 YAML frontmatter 解析结果"""
    name: str
    description: str
    keywords: tuple[str, ...] = ()
    lifecycle: SkillLifecycle = SkillLifecycle.PERSISTENT
    deactivate_on: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)
    always_load: bool = False
    emoji: str = ""


# ═══════════════════════════════════════════════════════
#  L1: Skill 元数据（始终常驻内存）
# ═══════════════════════════════════════════════════════

@dataclass(frozen=True)
class SkillMeta:
    """L1: Skill 完整元数据，始终常驻内存"""
    # frontmatter
    name: str
    description: str
    keywords: tuple[str, ...]
    lifecycle: SkillLifecycle
    deactivate_on: tuple[str, ...]
    always_load: bool
    emoji: str
    extra: dict[str, Any]
    # _meta.json
    slug: str
    version: str
    # 文件系统
    skill_dir: Path
    skill_md_path: Path
    has_scripts: bool
    has_references: bool
    script_paths: tuple[str, ...] = ()
    reference_paths: tuple[str, ...] = ()


# ═══════════════════════════════════════════════════════
#  L2: Skill Body（触发后加载）
# ═══════════════════════════════════════════════════════

@dataclass(frozen=True)
class SkillBody:
    """L2: SKILL.md body 内容"""
    skill_name: str
    content: str
    token_count: int = 0
    source_path: Path = field(default_factory=Path)


# ═══════════════════════════════════════════════════════
#  L1.5: 脚本信息（触发后构建，内容不预加载）
# ═══════════════════════════════════════════════════════

@dataclass(frozen=True)
class SkillScript:
    """scripts/ 下的单个脚本信息"""
    skill_name: str
    relative_path: str
    absolute_path: Path
    language: ScriptLanguage

    @staticmethod
    def detect_language(path: Path) -> ScriptLanguage:
        suffix_map = {
            ".py": ScriptLanguage.PYTHON,
            ".sh": ScriptLanguage.SHELL,
            ".cjs": ScriptLanguage.NODE,
            ".js": ScriptLanguage.NODE,
            ".mjs": ScriptLanguage.NODE,
        }
        return suffix_map.get(path.suffix, ScriptLanguage.PYTHON)


# ═══════════════════════════════════════════════════════
#  L3: 引用文档（按需加载）
# ═══════════════════════════════════════════════════════

@dataclass(frozen=True)
class SkillReference:
    """references/ 下的单个文档信息"""
    skill_name: str
    relative_path: str
    absolute_path: Path
    content: str = ""


# ═══════════════════════════════════════════════════════
#  聚合: 已加载的完整 Skill
# ═══════════════════════════════════════════════════════

@dataclass(frozen=True)
class LoadedSkill:
    """聚合对象：触发后完整加载的 Skill"""
    meta: SkillMeta
    body: SkillBody
    scripts: tuple[SkillScript, ...] = ()
    references: tuple[SkillReference, ...] = ()

    @property
    def name(self) -> str:
        return self.meta.name

    @property
    def skill_dir(self) -> Path:
        return self.meta.skill_dir

    def get_script(self, relative_path: str) -> SkillScript | None:
        for s in self.scripts:
            if s.relative_path == relative_path:
                return s
        return None

    def get_reference(self, relative_path: str) -> SkillReference | None:
        for r in self.references:
            if r.relative_path == relative_path:
                return r
        return None

    def to_prompt_block(self) -> str:
        return f"## Skill: {self.name}\n{self.body.content}"


# ═══════════════════════════════════════════════════════
#  缓存
# ═══════════════════════════════════════════════════════

class SkillCache:
    """Skill 内容缓存（body + reference）"""

    def __init__(self, max_body_cache: int = 20, max_ref_cache: int = 10):
        self.body_cache: dict[str, SkillBody] = {}
        self.ref_cache: OrderedDict[tuple[str, str], str] = OrderedDict()
        self.max_body_cache = max_body_cache
        self.max_ref_cache = max_ref_cache

    def get_body(self, name: str) -> SkillBody | None:
        return self.body_cache.get(name)

    def put_body(self, body: SkillBody):
        self.body_cache[body.skill_name] = body

    def get_ref(self, skill: str, ref: str) -> str | None:
        key = (skill, ref)
        if key in self.ref_cache:
            self.ref_cache.move_to_end(key)
            return self.ref_cache[key]
        return None

    def put_ref(self, skill: str, ref: str, content: str):
        key = (skill, ref)
        self.ref_cache[key] = content
        if len(self.ref_cache) > self.max_ref_cache:
            self.ref_cache.popitem(last=False)

    def invalidate(self, skill_name: str | None = None):
        if skill_name:
            self.body_cache.pop(skill_name, None)
            self.ref_cache = OrderedDict(
                (k, v) for k, v in self.ref_cache.items() if k[0] != skill_name
            )
        else:
            self.body_cache.clear()
            self.ref_cache.clear()
```
