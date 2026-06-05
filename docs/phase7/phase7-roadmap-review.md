# Phase 7 开发计划审计报告

> 审计人：开发架构师
> 审计日期：2026-06-05
> 审计对象：`docs/phase7/phase7-roadmap.md` — "Skill 系统完善"
> 代码基线：P6 完成（MCP 协议集成、McpToolHandler、/mcp 命令已落地）
> 当前状态：蓝图阶段——旧 `skills/loader.py`（4 字段 Skill + 手工解析）将被 scanner/registry/models 完全替代
> **查阅状态：已查阅 ✅（2026-06-05）**

---

## 总体评价

Phase 7 计划设计精炼。Scanner（扫描）→ Registry（索引）→ Provider（注入）三层分离清晰，渐进式披露模型（L1 描述常驻 → L2 body 按需 → L3 scripts 按需）是 LLM 驱动 Skill 系统的正确架构。**复用 `read_file` 而不新增工具**是架构克制的好例子。存在 **1 个结构性缺陷**和 **3 个设计缝隙**。

---

## 一、结构性缺陷（🔴 阻塞级）

### 缺陷 1：`SkillsConfig.enabled` 与 `ToolsConfig.skill_enabled` 存在双重开关

**位置**：§4.1、P5 遗留

**问题描述**：

当前 P5 `ToolsConfig` 中已有：

```python
class ToolsConfig:
    skill_enabled: bool = True   # Phase 5 预留，暂不消费
```

Phase 7 在 `SkillsConfig` 中新增了独立的：

```python
class SkillsConfig:
    enabled: bool = True          # 是否启用 Skill 系统
```

现在有**两个布尔值**控制 Skill 系统的启停：

| 位置 | 字段 | 值 |
|------|------|-----|
| `config.tools.skill_enabled` | P5 预留 | `True` |
| `config.skills.enabled` | P7 新增 | `True` |

`main.py` 的初始化代码检查 `config.skills.enabled`。但如果用户将 `config.tools.skill_enabled` 设为 `false`（P5 文档暗示这里控制 Skill 启停），Skill 系统**仍然会初始化**——两个开关不同步。

**影响**：用户困惑——有两个地方控制 Skill 启停，但只有一个生效。

**改进建议**：

以下二选一：

- **方案 A（推荐）**：明确 `config.skills.enabled` 是**唯一权威源**。在路线图中标注 `config.tools.skill_enabled` 为 "P5 遗留，P7 起废弃，不消费"。同时在 `_raw_to_config()` 中加注释：`# skill_enabled: P7 起由 SkillsConfig.enabled 替代，此字段仅保留向后兼容不消费`

- **方案 B**：在 main.py 中使用 OR 逻辑：`if config.skills.enabled and config.tools.skill_enabled:`。但这样会让两个开关都生效，反而增加了复杂度。

建议方案 A。两个独立开关只会制造困惑。

---

## 二、设计缝隙（🟡 重要）

### 缝隙 2：`SkillLifecycle.ONE_SHOT` 和 `EPHEMERAL` 定义但永不被消费

**位置**：§4.2、§10

**问题描述**：

```python
class SkillLifecycle(StrEnum):
    PERSISTENT = "persistent"
    ONE_SHOT = "one-shot"
    EPHEMERAL = "ephemeral"
```

`ONE_SHOT` 和 `EPHEMERAL` 在 `SkillMeta` 中被解析和存储，但 Phase 7 的 "不做的事项"（§10）明确排除了生命周期管理：

> "当前无框架主动管理 Skill 生命周期的需求"

这意味着 `ONE_SHOT` 和 `EPHEMERAL` 在 P7 中仅仅是**被解析、被存储、被忽略**的值——LLM 不知道它们的生命周期状态，SkillsProvider 也不根据它们做任何过滤。

**影响**：开发者看到 `SkillLifecycle` 枚举有三个值，会期望系统支持三种模式。但实际上只有一个生效。

**改进建议**：

- **方案 A（轻量）**：在 §10 或 §4.2 中明确标注："`ONE_SHOT` 和 `EPHEMERAL` 在 P7 中仅定义，不消费。生命周期管理留待后续 Phase。"

- **方案 B（推荐）**：将 `ONE_SHOT` 和 `EPHEMERAL` 标记为注释中的预留值，不在 P7 的 SKILL.md 规范中暴露。当前只支持 `PERSISTENT`。

建议方案 B，不在文档层面暴露未来功能，避免用户期望与实际能力不匹配。

---

### 缝隙 3：Frontmatter 正则不支持 Windows 换行符

**位置**：§4.3 — `_parse_frontmatter()`

**问题描述**：

```python
match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
```

这个正则假设换行符是 `\n`，但在 Windows 上，文本文件可能有 `\r\n` 换行。如果 SKILL.md 以 `\r\n` 存储（Windows Git 默认行为），正则匹配会失败。

```python
# 失败案例（\r\n 换行）：
"---\r\nname: test\r\n---\r\n"  → 正则的 \n 匹配不到 \r\n
```

**影响**：Windows 用户创建的 SKILL.md 可能被跳过（"SKILL.md 无有效 frontmatter"）。

**改进建议**：

```python
# 修改正则，支持 \r\n 和 \n
match = re.match(r'^---\s*\r?\n(.*?)\r?\n---\s*\r?\n', content, re.DOTALL)
```

或更稳健的方式——先规范化换行符：

```python
content = content.replace('\r\n', '\n')  # 规范化换行
match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
```

在 §4.3 的 `_parse_frontmatter()` 方法中增加此处理。

---

### 缝隙 4：`SkillsProvider` 生成的 prompt 文本可能过长

**位置**：§4.5

**问题描述**：

`SkillsProvider.provide()` 生成的 prompt 固定长度约 250 字（mandatory 说明），加上每个 Skill 的一行描述。当 Skill 数量较多时（如 20+ 个），Skill 列表本身可能成为 prompt 噪音，挤占其他 section（工具列表、记忆上下文）的 token 预算。

当前设计中，所有 Skill 的描述无条件注入 system prompt，没有基于用户 query 的过滤或截断机制。

**影响**：Skill 数量增长后，system prompt 膨胀。20 个 Skill × 每行 ~60 字 = ~1200 字 ≈ ~300 tokens（中文），影响可控但值得关注。

**改进建议**：

在当前阶段不需要修改——P7 的 Skill 数量预计在个位数，token 影响可忽略。但建议在 §4.5 末尾加一条前瞻注释：

> "Skill 数量超过 20 时，可考虑在 SkillsProvider 中基于用户 query 做关键词预过滤，仅注入匹配的 Skill 描述。当前 P7 范围暂不处理。"

---

## 三、前瞻性审查

### 长期发展性评估

| 维度 | 评分 | 说明 |
|------|------|------|
| **P8 Scheduler 触发 Skill** | ✅ 好 | Skill 通过 read_file 激活与 Scheduler 无关——独立正交 |
| **P9 取消机制** | ✅ 好 | Skill 执行通过 exec tool，已有超时+cancel 机制 |
| **P10 多渠道** | ✅ 好 | Skill 注入是纯文本，不依赖 channel 类型 |
| **Skill 热加载** | ⚠️ 中等 | P7 明确不实现（§10），但扫描器设计天然支持重新扫描 |
| **跨 Skill 编排** | ✅ 好 | LLM 驱动的 read_file 模型天然支持链式调用多个 Skill |
| **Skill 版本管理** | ⚠️ 中等 | SkillMeta 无 version 字段——当前 Skill 由文件系统管理，版本不追踪 |

### 架构亮点

1. **渐进式披露**：L1 描述常驻（~60 字）→ L2 body 按需 read_file（~500-5000 字）→ L3 scripts/references 按 LLM 判断——三级披露是 LLM 上下文管理的经典模式
2. **复用不新建**：Skill 激活完全通过现有 `read_file` 工具，不加新工具——接口面积不增长
3. **frontmatter 全量解析**：`SkillMeta` 包含 20+ 字段 + `extra` 兜底，为后续扩展留足空间
4. **解析容错**：无效 lifecycle 降级、无效 frontmatter 跳过、扫描异常不中断——健壮性好
5. **内存友好**：10 个 Skill 仅 ~6.5KB 常驻内存，100 个也才 ~65KB
6. **LLM 自主激活**：框架不做匹配/路由/规则引擎——LLM 是最强的意图理解器，把判断权交给它就是最简洁的设计

---

## 四、缺失项清单

| # | 缺失项 | 影响 | 建议 |
|---|--------|------|------|
| 1 | `ToolsConfig.skill_enabled` 废弃标注 | 🟡 双重开关困惑 | 在路线图或代码中标注 "P5 遗留，P7 废弃" |
| 2 | `SkillLifecycle.ONE_SHOT/EPHEMERAL` 消费 | 🟡 枚举值定义但未使用 | 标注为预留或移除（见缝隙 2） |
| 3 | `_scan_subdir()` 测试覆盖 | 🟢 辅助方法 | 单元测试中已通过 scanner 测试间接覆盖，可接受 |
| 4 | `metadata.openclaw` 命名空间说明 | 🟢 继承自 CowAgent 约定，dotClaw 语境下可能困惑 | 在 SKILL.md 规范中加注 "openclaw 命名空间为历史约定" |

---

## 五、改进建议汇总

### 必须在编码前修正

| # | 建议 | 影响位置 |
|---|------|---------|
| 1 | 明确 `SkillsConfig.enabled` 是唯一权威源，标注 `ToolsConfig.skill_enabled` 废弃 | §4.1, P5 遗留 |

### 建议在编码前确认

| # | 建议 | 影响位置 |
|---|------|---------|
| 2 | 标注 `ONE_SHOT`/`EPHEMERAL` 为预留值或移除 | §4.2, §10 |
| 3 | Frontmatter 正则支持 `\r\n` 换行 | §4.3 |
| 4 | SkillsProvider 加入 Skill 数量增长时的 token 预测注释 | §4.5 |

---

## 六、与数据设计文档的一致性检查

路线图 §0 引用了 `docs/phase7/skill-dataclass-design.md` 作为数据结构设计依据。路线图中的 `SkillMeta`、`SkillLifecycle`、`SkillScanner`、`SkillRegistry` 描述与该设计文档一致。两文档之间未发现矛盾。

---

> **给计划人员的行动项**：唯一阻塞级问题是缺陷 1（双开关），修改后即可启动开发。Phase 7 的 "复用 read_file 不新增工具" 策略是六个 Phase 中最能体现架构克制的一处——推荐保留。

---

## 计划修正回执

> 查阅人：dotclaw开发工程师
> 查阅日期：2026-06-05
> 修正版本：phase7-roadmap.md（已同步更新）

### 逐条处理

| # | 审计项 | 判定 | 处理 | 理由 |
|---|--------|------|------|------|
| 缺陷1 | `SkillsConfig.enabled` 与 `ToolsConfig.skill_enabled` 双开关 | ✅ 同意 | 已修正 §4.1：明确 `SkillsConfig.enabled` 为唯一权威源，`ToolsConfig.skill_enabled` 标注为 P5 遗留不消费。§10 不做事项新增对应条目 | 方案A合理，双开关制造困惑 |
| 缝隙2 | `ONE_SHOT`/`EPHEMERAL` 定义但未消费 | ⚠️ 部分同意 | 已修正 §4.2 SkillLifecycle 加注释"P7 仅定义不消费"，§10 不做事项新增条目。**未移除枚举值** | frontmatter 可能包含 lifecycle: one-shot，移除枚举值后 _parse_lifecycle() 降级逻辑无法正确解析目标值。保留枚举+文档标注是正确策略 |
| 缝隙3 | Frontmatter 正则不支持 `\r\n` | ✅ 同意 | 已修正 §4.3 `_parse_frontmatter()` 入口增加 `content.replace('\r\n', '\n')` 规范化。§9.3 边界测试新增测试项 | 规范化换行符比改正则更干净 |
| 缝隙4 | SkillsProvider prompt 可能过长 | ✅ 同意 | 已在 §4.5 末尾加前瞻注释：Skill 数量超过20时可考虑预过滤，当前暂不处理 | 当前规模不构成问题，加注释足够 |

### 结论

四项审计建议全部已处理，唯一阻塞级问题（缺陷1）已修正。可以启动开发。
