# Phase 7 代码审查报告

> 审查日期：2026-06-05
> 审查范围：Phase 7 Skill 系统完善（数据层→扫描+注册→集成层→启动+CLI，全部完成）
> 审查基准：`docs/phase7/phase7-roadmap.md` 设计文档 + `docs/prompt/code-review-prompt.md` 审查标准
> 测试状态：127/127 全部通过（Phase 1-6 回归 95 + Phase 7 验收 32）

---

## 审查总览

Phase 7 Skill 系统完善从骨架占位升级为完整链路：启动时自动扫描 skills 目录 → 解析 SKILL.md frontmatter → 构建 SkillMeta → 注册到 SkillRegistry → 注入 AgentContext → SkillsProvider 生成 system prompt 描述块 → LLM 自主选择并读取执行。架构设计精准复用 Phase 3 预留的 DataProvider 接口和 Phase 7 预留的 SkillsProvider 类，四个新增文件（models/scanner/registry/`__init__`）职责清晰、依赖单向。SkillMeta 作为 frozen dataclass 保证了多线程安全，33 个字段全量 frontmatter + 文件系统字段覆盖为后续扩展预留了充足空间。

无 Critical 问题。发现 1 个 Warning 和 5 个 Minor 问题。

| 严重级别 | 数量 | 说明 |
|----------|------|------|
| Critical | 0 | — |
| Warning | 1 | 建议修复 |
| Minor | 5 | 可后续改进 |

### 修复记录（2026-06-05）

全部 Warning + Minor 已修复，127/127 回归通过。

| 编号 | 严重级别 | 修复状态 | 涉及文件 |
|------|----------|----------|----------|
| W1 | Warning | ✅ 已修复 | skills/scanner.py — _walk + _scan_subdir 使用 follow_symlinks=False + is_symlink 检查 |
| M1 | Minor | ✅ 已修复 | skills/scanner.py — _parse_frontmatter 增加  规范化 |
| M2 | Minor | ✅ 已修复 | skills/models.py + registry.py + main.py — SkillMeta.truncated_description 消除重复逻辑 |
| M3 | Minor | ✅ 已修复 | skills/registry.py — register() 添加 debug 级别覆盖日志 |
| M4 | Minor | ✅ 已修复 | skills/registry.py + main.py — max_desc_len 从 20 提升到 40 |
| M5 | Minor | ✅ 已修复 | skills/scanner.py — _parse_skill 中 description 为空时 hint 日志 |
| Info | 3 | 可选优化 |

---

## Warning — 建议修复

### W1. [scanner.py] 目录遍历存在符号链接循环风险，可能导致 RecursionError 崩溃

**位置**：
- `src/dotclaw/skills/scanner.py:53-66` — `_walk()` 递归目录遍历
- `src/dotclaw/skills/scanner.py:151-160` — `_scan_subdir()` 使用 `rglob("*")`

**问题描述**：

`_find_skill_files._walk()` 使用 `entry.is_dir()` 判断目录（Python 默认 follow_symlinks=True），然后递归进入。如果 skill 目录中存在符号链接循环（如 `a/ → b/ → a/` 的 symlink），会导致无限递归 → `RecursionError` → 启动崩溃。

`_scan_subdir()` 使用 `rglob("*")` 遍历 scripts/ 和 references/ 子目录，同样默认跟随符号链接，存在相同风险。

**风险**：启动时扫描崩溃导致 Agent 完全不可用。虽然在本地 CLI 场景下用户主动创建 symlink 循环的概率极低，但作为防御性编程，应该考虑。

**建议**：

```python
# _walk() 修复：使用 follow_symlinks=False
def _walk(path: Path):
    try:
        for entry in path.iterdir():
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

# _scan_subdir() 修复：rglob 禁用 symlink 跟随（Python 3.12+）
def _scan_subdir(self, skill_dir: Path, subdir_name: str) -> list[str]:
    subdir = skill_dir / subdir_name
    if not subdir.is_dir(follow_symlinks=False):
        return []
    paths: list[str] = []
    for f in subdir.rglob("*"):  # Python 3.12+ 默认 follow_symlinks=True
        if f.is_file() and not f.is_symlink():
            paths.append(str(f.relative_to(skill_dir)))
    return sorted(paths)
```

> **Python 版本兼容**：`is_dir(follow_symlinks=False)` 需要 Python 3.12+。dotclaw 的 `requires-python = ">=3.13"`，可以使用。

---

## Minor — 建议改进

### M1. [scanner.py] CR/LF 规范化不完整

**位置**：`src/dotclaw/skills/scanner.py:131`

**问题描述**：
`_parse_frontmatter()` 仅规范化 `\r\n` → `\n`（Windows 换行），未处理孤立的 `\r`（旧 Mac 格式）。虽然 `\r` 作为行终止符极其罕见，但在跨平台接收 SKILL.md 时理论上可能出现。

**建议**：添加 `content = content.replace('\r\n', '\n').replace('\r', '\n')`，覆盖全部三种换行符格式。

---

### M2. [registry.py + main.py] 描述截断逻辑重复

**位置**：
- `src/dotclaw/skills/registry.py:38-41` — `get_descriptions_block()`
- `src/dotclaw/skills/main.py:443-445` — `_cmd_skills()`

**问题描述**：
两处都有完全相同的"取第一行 + 截断"逻辑：

```python
first_line = desc.split("\n")[0].strip()
if len(first_line) > 20:
    first_line = first_line[:20] + "..."
```

**建议**：提取为共享工具方法（如 `SkillMeta.truncated_description(max_len: int = 20) -> str`），或在 `SkillRegistry` 中暴露一个 `get_skill_summaries()` 方法同时供两处使用。

---

### M3. [registry.py] `logger` 定义但未使用

**位置**：`src/dotclaw/skills/registry.py:11`

**问题描述**：
```python
logger = logging.getLogger("dotclaw.skills.registry")
```
定义后在整个文件中没有任何日志调用。ToolRegistry 的静默覆盖设计明确不需要警告（Phase 5 设计决策），但如果 registry 未来需要排障，缺少日志输入点。

**建议**：在 `register()` 方法中添加 debug 级别的覆盖日志：

```python
def register(self, meta: "SkillMeta") -> None:
    if meta.name in self._index:
        logger.debug(f"Skill 覆盖注册: {meta.name}")
    self._index[meta.name] = meta
```

---

### M4. [providers.py] `get_descriptions_block` 截断使用字符长度而非视觉宽度

**位置**：`src/dotclaw/skills/registry.py:40` + `src/dotclaw/skills/main.py:444`

**问题描述**：
`if len(first_line) > max_desc_len` 使用 Python `len()` 计算字符数。对于中文描述，"20个字符"实际只能容纳约10个汉字（CJK 字符视觉宽度 ≈ 2 倍 ASCII 字符），导致中文描述被过度截断。

当前 `max_desc_len=20` 是为英文设计的，对于中文 Skill 来说过短。

**建议**：对于中文场景，将 `max_desc_len` 预设为 40（或在 `get_descriptions_block` 中使用 `display_width()` 函数按 Unicode East Asian Width 计算）。Phase 7 scope 下至少将默认值调高到 30-40。

---

### M5. [models.py] `SkillMeta` 缺少 `description` 为空时的保护

**位置**：`src/dotclaw/skills/models.py:28`

**问题描述**：
`description: str` 在 frontmatter 中不是必填字段（只需要 `name` 必填）。如果 SKILL.md 中 `description: ""`（显式空字符串），SkillMeta 仍能创建，但 `get_descriptions_block()` 中 `first_line = meta.description.split("\n")[0].strip()` 会生成空字符串，显示为 `- **name**:  ` 格式。

**建议**：在 `_parse_skill()` 中增加 hint 日志：

```python
if not fm.get("description", "").strip():
    logger.debug(f"Skill {fm.get('name')} 的 description 为空，LLM 匹配可能受到影响")
```

---

## Info — 可选优化

### I1. [main.py:202-225] Skill 扫描同步阻塞启动

Phase 7 的 `scanner.scan()` 在 `_run_cli()` 主线程中同步执行。对于大规模 skill 目录（100+ skills），扫描+解析可能增加数百毫秒到数秒的启动延迟。而 Phase 6 的 MCP 连接已经使用了 `asyncio.create_task` 后台加载。

这是设计权衡——Skill 系统需要在 system prompt 构建前完成注册（PromptBuilder 在 AgentLoop 创建时同时初始化）。当前的同步设计保证了数据一致性，启动延迟在实际使用中可忽略（通常 skills 目录 < 20 个）。若未来 Skill 数量增长到 50+，可考虑将 scanner 移到后台并延迟 SkillsProvider 注册。

### I2. [providers.py:114-121] SkillsProvider 提示词硬编码中文

"技能系统（mandatory）"和"如果有技能的描述与用户需求匹配..."等提示词使用中文硬编码。如果 dotclaw 未来支持多语言 system prompt，需要提取到配置文件。

### I3. [scanner.py:103] `known_keys` 集合缺少 `summary` 等 Clawdbot 兼容字段

Clawdbot/OpenClaw 的 SKILL.md 规范可能包含 `summary`、`read_when` 等额外字段。当前 `known_keys` 集合仅包含 P7 规范定义的 8 个字段，其他字段会被归入 `extra`。这不影响功能，但意味着 `metadata` 内的 `openclaw.always`/`openclaw.emoji` 之外的 Clawdbot 兼容字段需要从 `extra` 重新提取，略微不够直观。

---

## 架构审查结论

### 符合设计文档 ✓

| 检查项 | 状态 | 说明 |
|--------|------|------|
| SkillMeta frozen dataclass 全量字段 | ✓ | 17 个字段，frontmatter + 文件系统双来源 |
| SkillLifecycle 三枚举 + 无效降级 | ✓ | PERSISTENT/ONE_SHOT/EPHEMERAL + `_parse_lifecycle()` 降级 |
| SkillsConfig 扩展（directory/enabled/skip_prefix） | ✓ | directory 支持 str\|list，_raw_to_config 后向兼容 |
| SkillScanner 递归扫描 + _ 跳过 | ✓ | `_find_skill_files._walk()` 递归 + skip_prefix 过滤 |
| SkillScanner yaml.safe_load 解析 | ✓ | 替代手工逐行解析，支持嵌套和多行值 |
| SkillScanner scripts/references 子目录扫描 | ✓ | `_scan_subdir()` + rglob 递归 |
| SkillRegistry CRUD + get_descriptions_block | ✓ | register/get/list_all + 描述块格式化 |
| SkillsProvider.provide() 实现 | ✓ | 从 context.skill_registry 读取 + 生成 mandatory 提示词 |
| AgentContext.skill_registry 字段 | ✓ | TYPE_CHECKING 导入，frozen dataclass |
| AgentLoop 接收 + _build_context 传入 | ✓ | `skill_registry` 参数 + context 构建 |
| main.py Skill 初始化链 | ✓ | SkillsConfig.enabled → Scanner → Registry → AgentLoop |
| /skills CLI 命令 | ✓ | 显示名称 + 截断描述 |
| SkillsProvider 注册到 PromptBuilder | ✓ | 在 main.py 中 `SkillsProvider()` 激活 |
| skills/loader.py 删除 | ✓ | 源文件确认不存在 |
| 回归测试全部通过 | ✓ | Phase 1-6 全部 95 tests 通过 |

### SOLID 原则评估

| 原则 | 评价 |
|------|------|
| **S — 单一职责** | ✓ SkillScanner 只管扫描（文件系统 I/O + 解析）、SkillRegistry 只管索引（内存 CRUD）、SkillsProvider 只管格式化（prompt 文本生成） |
| **O — 开闭原则** | ✓ 新增 frontmatter 字段只需在 SkillMeta 增加字段 + `_parse_skill()` 提取逻辑，不影响 scanner/registry/provider |
| **L — 里氏替换** | ✓ SkillsProvider 继承 DataProvider，行为符合接口契约（provide 返回 str\|None） |
| **I — 接口隔离** | ✓ DataProvider 仅两个抽象成员（section_name + provide），SkillsProvider 实现精确匹配 |
| **D — 依赖倒置** | ✓ PromptBuilder 依赖 DataProvider ABC；AgentContext 通过 TYPE_CHECKING 导入 SkillRegistry |

### 数据流一致性

| 路径 | 状态 | 验证 |
|------|------|------|
| config.yaml → SkillsConfig | ✓ | `_raw_to_config` 解析 directory/enabled/skip_prefix |
| SkillsConfig → SkillScanner | ✓ | scanner 构造参数直接对映 |
| SkillScanner.scan() → list[SkillMeta] | ✓ | `_parse_skill()` 构建完整 SkillMeta |
| SkillMeta → SkillRegistry.register() | ✓ | 按 name 索引 |
| SkillRegistry → AgentContext | ✓ | `AgentLoop._build_context()` 传入 |
| AgentContext → SkillsProvider | ✓ | `provide(context)` 读取 `context.skill_registry` |
| SkillsProvider → system prompt | ✓ | 返回格式化的 markdown 文本 |

---

## 测试覆盖评估

| 场景 | 测试数 | 覆盖内容 | 评价 |
|------|--------|----------|------|
| SkillMeta + SkillLifecycle | 4 | 创建/frozen/枚举值/全字段 | ✓ 充分 |
| SkillsConfig | 4 | 默认值/列表/解析/多目录 | ✓ 充分 |
| SkillScanner | 12 | 基础扫描/跳过_前缀/缺失目录/缺失name/无效lifecycle/递归/多目录/重名/多行描述/scripts+references/CRLF/keywords | ✓ 非常充分 |
| SkillRegistry | 5 | 注册/查询/列表/覆盖/描述块格式/空注册表 | ✓ 充分 |
| SkillsProvider | 3 | None registry/有数据/空注册表 | ✓ 充分 |
| AgentContext | 2 | skill_registry 字段/默认None | ✓ 覆盖 |
| 回归测试 | 2 | 核心导入/SkillsConfig 字段存在性 | ✓ 覆盖 |

**总计**：32 tests，覆盖 7 个场景，测试设计全面。亮点：
- 使用 `tempfile.TemporaryDirectory` 创建临时 skill 目录进行真实文件系统测试
- 覆盖了 CRLF 换行、无效 lifecycle 降级、多行 description 等前端未明确要求的边界情况
- 测试独立性好（每个测试创建独立的临时目录）

未覆盖但可后续增强的方面：
- SkillsProvider 多 Skill 描述块格式验证（当前只测 1 个 Skill）
- `always_load`/`deactivate_on`/`metadata.openclaw` 的跨字段完整解析测试
- `extra` 字段中 Clawdbot 兼容字段的提取验证

---

## 整体评价

Phase 7 Skill 系统完善工程质量优秀。核心设计决策——复用现有 `read_file` 工具而非新增 `read_skill_body` 工具、LLM 自主驱动匹配而非框架做关键词路由、SkillsProvider 精准叠加在 P3 预留的 DataProvider 接口上——体现了极强的设计克制和对已有架构的深度理解。

SkillScanner 的 12 个测试用例是本次审查的亮点：覆盖了 CRLF 规范化、无效 lifecycle 降级、多行 YAML 解析、嵌套递归扫描、重名跳过等场景，远超出路线图中列出的测试要点，展现了良好的测试先行意识。

发现的 1 个 Warning（symlink 循环风险）属于防御性编程改进，在正常使用场景下几乎不会触发，修复成本极低。5 个 Minor 问题可在后续迭代中优化，不阻塞当前交付。

**审查结论：通过，建议修复 W1 后合入主干。**
