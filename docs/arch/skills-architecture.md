# dotClaw Skill 系统架构文档

> **版本**: v1.0 | **对应 Phase**: P7 | **更新日期**: 2026-06-05
> **维护说明**: 本文档随 Skill 系统演进持续更新，每次架构变更请同步更新版本号和变更说明。

---

## 1. 架构总览

dotClaw Skill 系统采用**渐进式三层披露**架构——启动时扫描注册（L1 描述常驻）、运行时 LLM 按需读取（L2 body）、LLM 自主判断执行（L3 scripts/references）。复用现有 read_file 工具，不加任何新工具。

```
┌───────────────────────────────────────────────────────────────────┐
│                      dotClaw Agent 进程                           │
│                                                                   │
│  启动时（main.py）                                                │
│  ════════════════════════════════════════════════════════         │
│                                                                   │
│  ┌─────────────┐     ┌──────────────────┐     ┌──────────────┐   │
│  │ SkillsConfig │────►│  SkillScanner    │────►│SkillRegistry │   │
│  │ directory    │     │                  │     │  _index:{}   │   │
│  │ enabled      │     │  scan()          │     │  register()  │   │
│  │ skip_prefix  │     │   ├─ 递归扫描    │     │  list_all()  │   │
│  └─────────────┘     │   ├─ yaml解析    │     │  get_desc()  │   │
│                       │   ├─ symlink防护 │     └──────┬───────┘   │
│                       │   └─ subdir扫描  │            │            │
│                       └──────────────────┘            │            │
│                                                       ▼            │
│                                               AgentContext        │
│                                            .skill_registry        │
│                                                       │            │
│                                                       │            │
│  每次请求（AgentLoop.run()）                      │            │
│  ════════════════════════════════════════════════════════         │
│                                                       │            │
│  ┌───────────────┐    ┌─────────────────────────┐     │            │
│  │  _build_      │    │  PromptBuilder          │     │            │
│  │   context()   │    │  [Role][Rules][Tools]   │◄────┘            │
│  └───────┬───────┘    │  [Memory][Skills]       │                  │
│          │            └───────────┬─────────────┘                  │
│          │                        │                                │
│          │                        ▼                                │
│          │            ┌───────────────────────┐                    │
│          │            │  SkillsProvider       │                    │
│          └───────────►│  .provide(context)    │                    │
│                       │   → skill_registry    │                    │
│                       │   → 技能系统提示词     │                    │
│                       │   → 可用技能列表       │                    │
│                       └───────────────────────┘                    │
│                           │                                       │
│                           ▼                                       │
│                    system prompt 中包含:                          │
│                    ┌──────────────────────────┐                   │
│                    │ ## 技能系统（mandatory）  │                   │
│                    │ 使用 read_file 读取...    │                   │
│                    │ ### 可用技能              │                   │
│                    │ - **name**: desc `path`   │                   │
│                    └──────────────────────────┘                   │
│                                                                   │
│                                                                   │
│  LLM 交互                                                         │
│  ════════════════════════════════════════════════════════         │
│                                                                   │
│  LLM 看到 Skill 列表                                              │
│       │                                                           │
│       │  用户需求匹配 → 调 read_file(path="SKILL.md")             │
│       ▼                                                           │
│  ┌──────────────┐                                                 │
│  │  L2: SKILL.md │ ← body 全部内容（Markdown）                    │
│  │  使用指引      │    LLM 按 body 指令操作                        │
│  └──────┬───────┘                                                 │
│         │                                                         │
│         │  LLM 从 body 中获取:                                    │
│         ├── scripts/ 路径 → 调 exec 执行 ──────────┐              │
│         ├── references/ 路径 → 调 read_file 读取 ──┤              │
│         └── 直接按 body 指令操作 ──────────────────┘              │
│                   │                                               │
│                   ▼                                               │
│  ┌────────────────────────────────────┐                           │
│  │  L3: scripts/ & references/        │                           │
│  │  scripts/run.py  ← exec 工具执行   │                           │
│  │  references/guide.md ← read_file   │                           │
│  └────────────────────────────────────┘                           │
└───────────────────────────────────────────────────────────────────┘

        文件系统（skills/ 目录）
```

---

## 2. 渐进式三层披露数据流

```
L1: 描述常驻（启动时）
═══════════════════════════════════════════════════════════════

skills/ 目录
   │
   ▼
SkillScanner.scan()
   ├─ 递归扫描（跳过 _ 前缀，follow_symlinks=False）
   ├─ yaml.safe_load() 解析 SKILL.md frontmatter
   ├─ 构建 SkillMeta（17 字段，frozen dataclass）
   │
   ▼
SkillRegistry.register() × N
   │
   ▼
AgentContext.skill_registry ────────────── 全程可用，零延迟
   │
   ▼
SkillsProvider → system prompt:
   ## 技能系统（mandatory）
   - **xbrowser**: Browser auto... `D:\...\SKILL.md`
   - **hello**: 示例技能... `D:\...\SKILL.md`


L2: 按需读取（LLM 判断）
═══════════════════════════════════════════════════════════════

LLM 判断用户需求与 Skill 描述匹配
   │
   ▼
LLM 调用 read_file(path="D:\...\SKILL.md")
   │
   ▼
read_file 返回完整 SKILL.md 内容（含 frontmatter + body）
   │
   ▼
LLM 从 body 中获取：
   - 使用方式 / 触发条件
   - scripts/ 下的脚本路径
   - references/ 下的文档路径
   - 直接按 body 指令操作


L3: 按判断执行（LLM 决定）
═══════════════════════════════════════════════════════════════

LLM 从 L2 body 中获取资源路径：
   ├── scripts/run.py → LLM 调用 exec(path) 执行脚本
   ├── references/guide.md → LLM 调用 read_file(path) 读取文档
   └── 直接按 body 指令操作
```

---

## 3. 模块详解

### 3.1 `SkillMeta` + `SkillLifecycle` — 数据模型

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/skills/models.py` |
| **职责** | Skill 完整元数据（frontmatter 全量 + 文件系统字段），frozen dataclass 保证多线程安全 |

**`SkillLifecycle` 枚举**：

| 值 | 含义 | Phase |
|----|------|-------|
| `PERSISTENT` | 持久：触发后一直活跃 | P7 唯一消费 |
| `ONE_SHOT` | 一次性：完成后自动卸载 | 预留 |
| `EPHEMERAL` | 临时：每次请求重新判定 | 预留 |

**`SkillMeta` 完整字段**：

```
SkillMeta (frozen dataclass, ~500 B/Skill)
├── name: str                         # 必填，Skill 标识名
├── description: str                  # 必填，触发描述
├── keywords: tuple[str,...]          # 触发关键词
├── lifecycle: SkillLifecycle         # 生命周期模式
├── deactivate_on: tuple[str,...]     # 自动卸载条件
├── always_load: bool                 # metadata.openclaw.always
├── emoji: str                        # metadata.openclaw.emoji
├── homepage: str                     # 主页链接
├── author: str                       # 作者
├── metadata: dict                    # 扩展元数据
├── extra: dict                       # 兜底（未知字段）
├── skill_dir: Path                   # Skill 根目录
├── skill_md_path: Path               # SKILL.md 完整路径
├── has_scripts: bool                 # scripts/ 是否存在
├── has_references: bool              # references/ 是否存在
├── script_paths: tuple[str,...]      # scripts/ 相对路径清单
├── reference_paths: tuple[str,...]   # references/ 相对路径清单
└── truncated_description(max_len)    # M2 修复：共享截断方法
```

**内存占用**：10 个 Skill ≈ 6.5 KB（完全可忽略）。

---

### 3.2 `SkillScanner` — 扫描器

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/skills/scanner.py` |
| **职责** | 递归扫描 skills 目录，解析 SKILL.md frontmatter，构建 SkillMeta 列表 |

**`scan()` 完整流程**：

```
scan()
  ├─ 遍历 _skill_paths 中的每个根目录
  │    │
  │    ├─ _find_skill_files(base_path)
  │    │    └─ _walk(base_path)
  │    │         ├─ entry.is_dir(follow_symlinks=False)  ← W1 修复：symlink 防护
  │    │         ├─ 跳过 _ 前缀目录
  │    │         ├─ 发现 SKILL.md → 加入 results
  │    │         └─ 递归进入子目录
  │    │
  │    └─ 对每个 SKILL.md:
  │         │
  │         └─ _parse_skill(skill_md)
  │              ├─ 读取文件内容（encoding=utf-8）
  │              ├─ _parse_frontmatter(content)
  │              │    ├─ 规范化 \r\n → \n → \r → \n  ← M1 修复
  │              │    ├─ re.match(r'^---\s*\n(.*?)\n---\s*\n', ...)
  │              │    └─ yaml.safe_load(frontmatter)
  │              ├─ 校验 name 必填 → 否则跳过
  │              ├─ 校验 description 空 → debug hint  ← M5 修复
  │              ├─ _parse_lifecycle() → 无效值降级 PERSISTENT
  │              ├─ 提取 metadata.openclaw.always / emoji
  │              ├─ _scan_subdir("scripts")    → script_paths
  │              │    ├─ is_dir(follow_symlinks=False)  ← W1 修复
  │              │    └─ rglob + is_symlink 过滤
  │              ├─ _scan_subdir("references") → reference_paths
  │              └─ 返回 SkillMeta
  │
  ├─ 重名检测 → 跳过（first-win）
  └─ logger.info(f"共 {len(results)} 个 Skill")
```

**关键设计决策**：
- 递归遍历：找到 SKILL.md 后不停止递归（支持嵌套 Skill）
- yaml.safe_load()：替代手工逐行解析，支持嵌套字段和多行值
- 无效 lifecycle 降级 PERSISTENT：不跳过整个 Skill
- symlink 防护：Python 3.13 `follow_symlinks=False` + `is_symlink()` 双重防护
- 错误隔离：单个 Skill 解析失败不影响整体扫描

---

### 3.3 `SkillRegistry` — 注册表

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/skills/registry.py` |
| **职责** | SkillMeta 的索引容器 + 描述块生成 |

**对外方法**：

| 方法 | 输入 | 输出 | 说明 |
|------|------|------|------|
| `register(meta)` | `SkillMeta` | — | 同名覆盖 + debug 日志（M3 修复） |
| `get(name)` | `str` | `SkillMeta \| None` | 按名查询 |
| `list_all()` | — | `list[SkillMeta]` | 所有已注册 Skill |
| `get_descriptions_block(max_len)` | `int` | `str` | prompt 描述块（默认 40，CJK 兼容 M4） |

**`get_descriptions_block()` 输出格式**：

```markdown
- **xbrowser**: Browser automation... `D:\dev\dotclaw\skills\xbrowser\SKILL.md`
- **hello**: 示例技能：演示 Skill... `D:\dev\dotclaw\skills\_example\SKILL.md`
```

---

### 3.4 `SkillsProvider` — Prompt 注入

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/agent/prompt/providers.py` |
| **职责** | 从 `context.skill_registry` 读取 → 生成 system prompt 的技能 section |
| **接口** | 继承 `DataProvider` ABC，实现 `section_name = "skills"` + `provide(context) → str\|None` |

**`provide()` 逻辑**：

```
provide(context)
  ├─ [if not context.skill_registry] → return None  （跳过该 section）
  ├─ descriptions = registry.get_descriptions_block(max_desc_len=40)
  ├─ [if not descriptions] → return None
  └─ return f"""
       ## 技能系统（mandatory）
       如果有技能的描述与用户需求匹配：使用 read_file 工具读取...
       ### 可用技能
       {descriptions}
       """
```

**注入效果示例**：

```
## 技能系统（mandatory）

如果有技能的描述与用户需求匹配：使用 `read_file` 工具读取其路径的 SKILL.md 文件，
然后严格遵循文件中的指令。

**重要**: 技能不是工具，不能直接调用。使用技能的唯一方式是用 `read_file` 读取 SKILL.md 文件，
然后按文件内容操作。

### 可用技能

- **hello**: 示例技能：演示 Skill... `D:\dev\dotclaw\skills\_example\SKILL.md`
- **xbrowser**: EXCLUSIVE browser au... `D:\dev\dotclaw\skills\xbrowser\SKILL.md`
```

> **数据流**：`SkillsProvider.provide()` 是纯同步方法。扫描在 main.py 启动时完成，结果存入 `AgentContext.skill_registry`，`provide()` 只做格式化读取。这符合 `DataProvider` 接口的同步约束。

---

### 3.5 `SkillsConfig` — 配置扩展

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/config/settings.py` |
| **职责** | 控制 Skill 系统的启停、扫描路径、跳过规则 |

```python
@dataclass
class SkillsConfig:
    directory: str | list[str] = "./skills"   # 支持多目录
    enabled: bool = True                       # 是否启用 Skill 系统
    skip_prefix: str = "_"                     # 跳过此前缀开头的目录
```

**配置示例**（`config.yaml`）：

```yaml
skills:
  directory: ./skills
  enabled: true
  skip_prefix: "_"
```

---

## 4. 完整调用链路时序图

### 4.1 启动扫描 + Skill 注入

```
main.py           SkillScanner      SkillRegistry    AgentContext    SkillsProvider    PromptBuilder
 │                    │                  │                │               │                │
 ├─ scanner.scan() ──►                  │                │               │                │
 │                    │                  │                │               │                │
 │                    ├─ 遍历目录        │                │               │                │
 │                    ├─ 找到 SKILL.md   │                │               │                │
 │                    ├─ yaml.safe_load()│                │               │                │
 │                    ├─ 构建 SkillMeta  │                │               │                │
 │                    │                  │                │               │                │
 │◄── list[SkillMeta] ──                 │                │               │                │
 │                    │                  │                │               │                │
 ├─ for meta in metas:                  │                │               │                │
 │    registry.register(meta) ─────────►│                │               │                │
 │                    │                  │                │               │                │
 ├─ AgentLoop(skill_registry=registry)  │                │               │                │
 │                    │                  │                │               │                │
 │                    │                  │  ──────────► _build_context() │                │
 │                    │                  │   Ctx(skill_registry=reg)     │                │
 │                    │                  │                │               │                │
 │                    │                  │                ├─ PromptBuilder.build(ctx) ─────►
 │                    │                  │                │               │                │
 │                    │                  │                │               ├─ provide(ctx)  │
 │                    │                  │                │               │                │
 │                    │                  │                │               ├─ descriptions  │
 │                    │                  │                │               │◄── block ──────│
 │                    │                  │                │               │                │
 │                    │                  │                │               ├─ 技能系统提示词 │
 │                    │                  │                │               └─ 注入 prompt ──►
 │                    │                  │                │               │                │
 │                    │                  │                │               │    system prompt 含 Skill
```

### 4.2 LLM 自主激活 Skill

```
用户               LLM                 AgentLoop          read_file        exec_tool
 │                  │                     │                   │               │
 │── "打开浏览器" ──►│                     │                   │               │
 │                  │                     │                   │               │
 │                  │ system prompt 中有:  │                   │               │
 │                  │ "xbrowser: Browser.."│                   │               │
 │                  │                     │                   │               │
 │                  │ 判断匹配 xbrowser    │                   │               │
 │                  │                     │                   │               │
 │                  ├─ read_file(path="...xbrowser/SKILL.md") ─►              │
 │                  │                     │                   │               │
 │                  │◄── SKILL.md 全文 ────────────┤          │               │
 │                  │                     │                   │               │
 │                  │ body: "使用方式..."  │                   │               │
 │                  │ scripts/xbrowser.py │                   │               │
 │                  │                     │                   │               │
 │                  ├─ exec("python scripts/xbrowser.py") ───────────────────►
 │                  │                     │                   │               │
 │                  │◄── 执行结果 ────────────────────────────────────────────│
 │                  │                     │                   │               │
 │◄── "浏览器已打开" ──┤                    │                   │               │
```

---

## 5. 配置参考

`config.yaml` 中的 `skills:` 段：

```yaml
skills:
  # === Phase 7 新增 ===
  directory: ./skills              # 扫描根路径（支持字符串或列表）
  enabled: true                     # Skill 系统启用开关（唯一权威源）
  skip_prefix: "_"                  # 跳过此前缀开头的子目录

  # 多目录示例：
  # directory:
  #   - ./skills
  #   - ./extra-skills
```

**注意**：`ToolsConfig.skill_enabled`（P5 预留）不消费，以 `SkillsConfig.enabled` 为准。

---

## 6. 初始化链路（`main.py`）

```
main.py / _run_cli()
  │
  ├─ 1. load_config() → Config 对象
  │     └─ SkillsConfig（directory, enabled, skip_prefix）
  │
  ├─ 2. [if config.skills.enabled]
  │     │
  │     ├─ 解析 skill_paths（相对路径基于 project_root）
  │     │
  │     ├─ SkillScanner(skill_paths, skip_prefix)
  │     │    └─ scanner.scan()
  │     │         ├─ _find_skill_files → 递归遍历
  │     │         ├─ _parse_skill → yaml.safe_load → SkillMeta
  │     │         └─ 返回 list[SkillMeta]
  │     │
  │     ├─ SkillRegistry()
  │     │    └─ for meta in metas: registry.register(meta)
  │     │
  │     └─ skill_registry = registry
  │     else: skill_registry = None
  │
  ├─ 3. PromptBuilder([
  │       RoleProvider(), RulesProvider(), ToolsProvider(),
  │       MemoryProvider(), SkillsProvider(),
  │     ])
  │
  ├─ 4. AgentLoop(
  │       ...,
  │       skill_registry=skill_registry,
  │     )
  │
  └─ 5. 主循环：每次 run()
        └─ _build_context() → AgentContext(skill_registry=registry)
             └─ SkillsProvider.provide(context)
                  └─ 返回技能系统提示词 → system prompt
```

---

## 7. 后续扩展预留

| 扩展点 | 说明 |
|--------|------|
| SkillLifecycle.ONE_SHOT/EPHEMERAL | 枚举已定义，P7 不消费。生命周期管理（自动卸载/按请求判定）留待后续 |
| 创建向导 | `/skill create` CLI 基于 SkillMeta 字段生成 SKILL.md 模板 |
| 热加载 | watcher 监测 skills 目录变化 → 自动 re-scan → 热更新 registry |
| 条件加载 | frontmatter `requires` 字段已预留，P7 未消费 |
| 关键词预过滤 | SkillsProvider 中基于 query 关键词过滤 Skill 列表（>20 Skills 时考虑） |
| 多语言提示词 | SkillsProvider 的提示词当前硬编码中文，可提取到配置文件支持多语言 |

---

*本文档由 dotClaw 开发工程师维护。架构变更后请同步更新此文档。*
开发日志见 `docs/phase7/phase7-record.md`。详细设计见 `docs/phase7/phase7-roadmap.md`。
