# Phase 5 开发计划审计报告

> 审计人：开发架构师
> 审计日期：2026-06-03
> 审计对象：`docs/phase5-roadmap.md` — "工具层架构重构"
> 代码基线：P4 完成（三级记忆、SQLite FTS5+向量、Deep Dream 已落地）
> 上一轮审计：P4 v3 审计通过，已交付

---

## 总体评价

Phase 5 计划的架构方向正确——ToolRegistry 纯注册 / ToolExecutor 调度 / ToolHandler ABC 适配的三层分离是经典的关注点分离设计。合并 DebugManager 到 AgentLogger 也在正确时机关闭了 P3 遗留的技术债。但存在 **3 个结构性问题** 和 **5 个设计缝隙**，其中 #1（配置格式破坏性变更）是必须修正的。

---

## 一、结构性问题（🔴 阻塞级）

### 缺陷 1：`config.yaml` 格式破坏性变更，无迁移路径

**位置**：§4.11、§4.12

**问题描述**：

当前 config.yaml（P4 格式）：

```yaml
tools:
  exec:
    enabled: true
    needs_approval: true
  python:
    enabled: true
    needs_approval: true
    timeout: 30
  web_search:
    enabled: false
```

Phase 5 计划的新格式（§4.12）：

```yaml
tools:
  builtin_enabled: true
  mcp_enabled: true
  skill_enabled: true
  approval_commands:
    - exec
    - python
  exec_timeout: 60
  web_search:
    enabled: false
```

这两个格式**完全不兼容**。用户升级到 P5 后，旧 `config.yaml` 的 `tools.exec.enabled`、`tools.exec.needs_approval` 等字段会被 `_raw_to_config()` 的新解析逻辑**静默忽略**——exec/python 工具的审批行为从"启用"变为"取决于 `approval_commands` 列表是否包含它们"。

**影响**：所有现有 dotClaw 安装的 config.yaml 需要手动迁移。不迁移 → 审批机制行为变更。

**改进建议**：

在 `_raw_to_config()` 中增加**旧格式兼容逻辑**：

```python
# config/settings.py — _raw_to_config() 中的 ToolsConfig 解析
tools_raw = raw.get("tools", {})

# 兼容旧格式：如果存在旧 keys，自动转换为 approval_commands
approval_commands = tools_raw.get("approval_commands", [])
if not approval_commands:
    # Backward compat: old format per-tool needs_approval
    for tool_name in ("exec", "python"):
        if tools_raw.get(tool_name, {}).get("needs_approval", False):
            approval_commands.append(tool_name)

tools = ToolsConfig(
    builtin_enabled=tools_raw.get("builtin_enabled", True),
    mcp_enabled=tools_raw.get("mcp_enabled", True),
    skill_enabled=tools_raw.get("skill_enabled", True),
    approval_commands=approval_commands,
    exec_timeout=tools_raw.get("exec_timeout") or
                  tools_raw.get("python", {}).get("timeout", 60.0),
    web_search_enabled=tools_raw.get("web_search", {}).get("enabled", False),
)
```

同时在路线图 §4.12 补充兼容说明：

> "旧格式 `tools.exec.needs_approval: true` 自动转换为 `tools.approval_commands: [exec]`。用户升级到 P5 无需手动修改 config.yaml。旧格式的 `exec.enabled` / `python.enabled` 被 `builtin_enabled` 替代——如需禁用单个工具，未来版本通过 ToolDefinition 级别的 enable/disable 属性支持。"

---

### 缺陷 2：`builtin_enabled` 替代了 `exec_enabled` / `python_enabled` 的细粒度控制

**位置**：§4.11、§4.12

**问题描述**：

当前 `ToolsConfig` 支持按工具名分别启用/禁用：

| 旧字段 | 含义 |
|--------|------|
| `exec_enabled` | 单独控制 exec 工具 |
| `python_enabled` | 单独控制 python 工具 |

Phase 5 计划的新 `ToolsConfig` 只有 source 级启停：

| 新字段 | 含义 |
|--------|------|
| `builtin_enabled` | 所有内置工具（exec + file_* + memory_* + system_*）全部启用或全部禁用 |

这意味着用户无法只为 `exec` 禁用审批——要么全部内置工具启用，要么全部禁用。这是功能退化。

**影响**：用户无法做细粒度工具管控。对于安全性敏感的场景（如只想禁止 exec 但保留 file 工具），这不可接受。

**改进建议**：

以下三选一：

- **方案 A（推荐，最小改动）**：保留 `approval_commands` 从 config 加载的同时，`exec_timeout` 保留 per-tool 超时。对于禁用单个工具的场景，让 `ToolRegistry.unregister(name)` 承担——main.py 初始化时根据旧 config 的 `exec_enabled: false` 调用 `tool_registry.unregister("exec")`。

- **方案 B**：在 `ToolsConfig` 中增加 `disabled_tools: list[str]` 字段，列出需要禁用的工具名。初始化时 `ToolRegistry` 根据此列表注销工具。

- **方案 C**：接受功能退化，在升级说明中注明"P5 起内置工具统一管理，如需细粒度控制请提 issue / 使用 unregister() API"。

建议 **方案 A**，用最小代码量保持兼容。

---

### 缺陷 3：审批双重检查机制文档不清晰

**位置**：§4.4、§4.5

**问题描述**：

审批流程经过**两道门**：

| 位置 | 检查内容 | 作用 |
|------|---------|------|
| `ToolExecutor.execute()` (§4.4) | `if definition.needs_approval:` → 调用 `approval.check()` | 第一道门：工具声明自己危险 |
| `ApprovalManager.check()` (§4.5) | `if tool_name not in self._approval_commands: return True` | 第二道门：用户配置审批列表 |

两道门的关系是 AND —— 必须两者都通过才会触发用户确认。但路线图 §10.6 声称"Phase 5 先实现 approval_commands，needs_approval 字段预留"，暗示只有一道门生效。这与代码实现矛盾。

正确的当前行为：
- `exec` 工具：`needs_approval=True`（在 `get_exec_handler()` 中设置）→ 进入 `check()` → 检查 `approval_commands` 包含 `exec` → 触发审批 ✅
- 如果 `approval_commands` 中移除 `exec`：`needs_approval=True` → 进入 `check()` → 不在列表中 → `return True`（放行）⚠️
- 如果 `get_exec_handler()` 中设置 `needs_approval=False`：executor 不调用 `check()` → 直接执行 ⚠️

**影响**：开发者不清楚到底应该在哪里控制审批——修改工厂函数中的 `needs_approval`，还是修改 `config.yaml` 的 `approval_commands`？

**改进建议**：

在路线图 §4.4 或 §10.6 中增加审批流程的精确说明：

```
审批完整流程：
1. ToolExecutor 检查 ToolDefinition.needs_approval
   - False → 跳过审批，直接执行
   - True  → 进入步骤 2
2. ApprovalManager.check() 检查 tool_name in approval_commands
   - 不在列表中 → 放行（ApprovalManager 信任工具不会被误标）
   - 在列表中   → 进入步骤 3
3. 通过 channel.ask_user() 请求用户确认
   - y/yes → 执行
   - 其他  → 返回 APPROVAL_DENIED
```

---

## 二、设计缝隙（🟡 重要）

### 缝隙 4：`_cmd_tools()` 中 `[需审批]` 标记未更新

**位置**：§4.10（main.py 修改未提及 `_cmd_tools`）

**问题描述**：

当前 `main.py` 的 `_cmd_tools()` 函数通过硬编码检查标记审批工具：

```python
if d.name == "exec" and tool_registry._config.tools.exec_needs_approval:
    mark = " [需审批]"
elif d.name == "python" and tool_registry._config.tools.python_needs_approval:
    mark = " [需审批]"
```

Phase 5 后 `exec_needs_approval` 和 `python_needs_approval` 字段不存在了，这段代码会抛出 `AttributeError`。

**改进建议**：

更新 `_cmd_tools()` 使用新的数据源。有两种方式：

```python
# 方式 A：从 ToolDefinition.needs_approval 读取（推荐）
for d in definitions:
    handler = tool_registry.get(d.name)
    if handler and handler.definition().needs_approval:
        mark = " [需审批]"

# 方式 B：从 config.tools.approval_commands 检查
for d in definitions:
    if d.name in config.tools.approval_commands:
        mark = " [需审批]"
```

建议方式 A（从 Handler 的 definition 读取），因为审批状态的权威来源是工具定义，不是 config 列表。

---

### 缝隙 5：`needs_approval` 在 executor 中是 first gate，但 handler 工厂函数中的默认值是 `False`

**位置**：§4.2、§4.4

**问题描述**：

`BuiltinToolHandler.__init__()` 的 `needs_approval` 默认值是 `False`（§4.2）。只有 `exec_tool.py` 的工厂函数显式设为 `True`。其他 7 个内置工具的工厂函数接受默认值 `False`。

`ToolExecutor.execute()` 的审批检查（§4.4）：

```python
if definition.needs_approval:   # ← 默认 False，对大多数工具不触发
    approved = await self._approval.check(...)
```

如果开发者新增一个危险工具但忘记设置 `needs_approval=True`，该工具会**静默绕过审批**——安全的默认值 (`False`) 反而变成了安全隐患。

**改进建议**：

在路线图 §4.2 的 `BuiltinToolHandler` 注释中增加警告：

> "needs_approval 默认值为 False（安全优先——不审批不过度拦截）。开发者新增危险工具时必须显式设置 needs_approval=True。建议在 code review 中检查新增工具的 needs_approval 设置。"

或在 `register_all()` 中增加运行期检查：打印所有 `needs_approval=True` 的工具名到 info 日志。

---

### 缝隙 6：`ToolProvider.discover_and_register()` 参数未标注类型

**位置**：§4.7

**问题描述**：

```python
class ToolProvider(ABC):
    async def discover_and_register(self, registry) -> list[str]:
        ...
```

`registry` 参数缺少类型标注。作为公共 ABC 接口，类型标注是 API 文档的一部分——后续 Phase 6/7 实现 MCPToolProvider 和 SkillToolProvider 时，开发者需要知道 `registry` 接受什么类型。

**改进建议**：

```python
from .registry import ToolRegistry

class ToolProvider(ABC):
    async def discover_and_register(self, registry: ToolRegistry) -> list[str]:
        ...
```

---

### 缝隙 7：验收场景编号错乱 + 测试数量不一致

**位置**：§8.1、§8.3

**问题描述**：

§8.1 验收场景编号为：场景 1 → 场景 2 → 场景 3 → **场景 4 → 场景 6 → 场景 5**。场景 5 和 6 的顺序颠倒了。

§8.3 回归验收写道："tests/test_phase5_acceptance.py 全部通过（**8 个场景**）"，但 §7 测试计划列出了 **9 个场景**（#1-#9）。8 ≠ 9。

**改进建议**：修正场景编号顺序（4→5→6），统一测试数量为 9。

---

### 缝隙 8：`NEEDS_APPROVAL` 常量删除后的外部引用检查

**位置**：§4.5

**问题描述**：

当前 `tools/approval.py` 导出模块级常量 `NEEDS_APPROVAL = {"exec", "python"}`。Phase 5 计划删除它，但没检查代码库中是否有其他地方 `import NEEDS_APPROVAL`。

排查结果（基于当前代码）：没有外部文件直接导入 `NEEDS_APPROVAL`（仅在 approval.py 内部使用），但路线图应注明已验证此点。

**改进建议**：在 §4.5 末尾补充："`NEEDS_APPROVAL` 无外部引用（grep 确认），可直接删除。"

---

## 三、前瞻性审查

### 长期发展性评估

| 维度 | 评分 | 说明 |
|------|------|------|
| **P6 MCP 集成兼容** | ✅ 好 | `ToolProvider` ABC + `ToolHandler` ABC 已预留接口，`ToolSource.MCP` 枚举已定义 |
| **P7 Skill 工具化兼容** | ✅ 好 | `ToolSource.SKILL` 枚举已定义，`SkillToolHandler` 只需实现 `ToolHandler` ABC |
| **工具热加载** | ✅ 好 | `ToolRegistry.unregister()` + `register()` 已预留，运行时动态注册路径通畅 |
| **工具超时精细化** | ✅ 好 | `ToolExecutionContext` 目前只有 timeout，未来可扩展 memory_limit 等 |
| **多渠道审批** | ⚠️ 中等 | `channel=None` 时默认放行——Scheduler/子 Agent 场景下安全。但 P10 Web Channel 下审批交互方式不同，需届时适配 |
| **工具权限模型** | ⚠️ 中等 | `source` 级 + `approval_commands` 给足了基础粒度。但按用户/会话的细粒度权限在 Phase 5 明确标记为 out of scope——正确 |

### 架构亮点

1. **三层分离**：Registry（注册）→ Executor（调度+审批+超时）→ Handler（执行）——职责清晰，每一层可独立测试
2. **BuiltinToolHandler Adapter**：不改现有工具函数签名，包装器模式让迁移零风险
3. **合并日志系统**：DebugManager → AgentLogger 的合并关闭了 P3 遗留的技术债——时机选择正确
4. **ToolSource 枚举**：builtin/mcp/skill/custom 四级分类，为后续按来源启停和过滤打好了基础
5. **Phase 5 边界清晰**：§11 明确列出 6 项 out of scope——避免范围蔓延

---

## 四、缺失项清单

| # | 缺失项 | 影响 | 建议 |
|---|--------|------|------|
| 1 | config.yaml 旧格式迁移路径 | 🔴 现有安装静默失效 | 在 _raw_to_config() 中增加旧格式兼容（见缺陷 1） |
| 2 | `_cmd_tools()` 中需要审批标记的更新 | 🟡 /tools 命令报错 | 改为从 ToolDefinition.needs_approval 读取（见缝隙 4） |
| 3 | per-tool 启用/禁用能力保留方案 | 🔴 功能退化 | main.py 根据旧 config 调用 unregister()（见缺陷 2） |
| 4 | `NEEDS_APPROVAL` 外部引用审计 | 🟢 确认无残留 | grep 确认无外部引用后删除 |
| 5 | 审批流程文档（两道门 AND 关系） | 🟡 开发者理解困难 | 补充完整流程图（见缺陷 3） |
| 6 | `builtin/__init__.py` 中 `get_python_handler` 缺失 | 🟡 当前 8 个工具中无 python 工具 | 确认是否需要；当前 tools/ 下无 python_tool.py |

---

## 五、改进建议汇总

### 必须在编码前修正（阻塞 P5 开发启动）

| # | 建议 | 影响位置 |
|---|------|---------|
| 1 | 在 `_raw_to_config()` 中增加旧 config.yaml 格式兼容逻辑 | §4.11, §4.12 |
| 2 | 保留 per-tool 启用/禁用能力（通过 unregister 或 disabled_tools 字段） | §4.11, §4.12 |
| 3 | 在路线图中补充审批双重检查机制的完整流程图和文档 | §4.4, §4.5, §10.6 |

### 建议在编码前确认（提升实施质量）

| # | 建议 | 影响位置 |
|---|------|---------|
| 4 | 更新 `_cmd_tools()` 中的审批标记逻辑 | §4.10 |
| 5 | ToolProvider.discover_and_register 参数加类型标注 | §4.7 |
| 6 | 修正验收场景编号（4→5→6）和测试数量（8→9） | §8.1, §8.3 |
| 7 | 确认 NEEDS_APPROVAL 无外部引用后标注 | §4.5 |
| 8 | BuiltinToolHandler 注释中强调 `needs_approval` 安全默认值风险 | §4.2 |

---

> **给计划人员的行动项**：优先解决缺陷 1（config 兼容）和缺陷 2（per-tool 控制），这是唯一会破坏现有安装的问题。缺陷 3（审批文档）是理解性问题，补充说明即可。整体而言，P5 计划架构清晰，工具层的三层分离设计（Registry/Executor/Handler）为 P6 MCP 和 P7 Skill 打下了坚实的基础。


---

## 审计回执

> 查阅人：dotclaw开发工程师
> 查阅日期：2026-06-03
> 状态：**已查阅**
> 对应文档版本：phase5-roadmap.md v1.2

### 修正情况

| # | 审计项 | 级别 | 判定 | 修正动作 | 修正位置 |
|---|--------|------|------|---------|---------|
| 缺陷 1 | config.yaml 格式破坏性变更 | 🔴 | ✅ 同意 | 增加 _raw_to_config() 旧格式兼容逻辑（needs_approval→approval_commands, enabled→disabled_tools） | §4.11, §4.12 |
| 缺陷 2 | builtin_enabled 替代 per-tool 控制 | 🔴 | ✅ 同意 | 采用方案 A：ToolsConfig 新增 disabled_tools 字段 + main.py 初始化时调用 unregister() | §4.11, §4.12, §4.10 |
| 缺陷 3 | 审批双重检查文档不清晰 | 🔴 | ✅ 同意 | §4.4 补充完整审批流程图（3 步），明确 AND 关系 | §4.4, §10.6 |
| 缝隙 4 | _cmd_tools() 审批标记需更新 | 🟡 | ✅ 同意 | §4.10 补充 _cmd_tools() 修改，从 handler.definition().needs_approval 读取 | §4.10 |
| 缝隙 5 | needs_approval 默认 False 安全风险 | 🟡 | ⚠️ 部分同意 | §4.2 增加警告注释，不建议 register_all() 打印日志（噪声问题） | §4.2 |
| 缝隙 6 | ToolProvider 参数缺类型标注 | 🟡 | ✅ 同意 | §4.7 registry 参数增加 "ToolRegistry" 类型标注 | §4.7 |
| 缝隙 7 | 验收编号错乱 + 测试数量不一致 | 🟡 | ✅ 同意 | 场景 5/6 顺序修正，§8.3 测试数量统一为 9 | §8.1, §8.3 |
| 缝隙 8 | NEEDS_APPROVAL 外部引用检查 | 🟢 | ✅ 同意 | §4.5 补充"无外部引用，可直接删除"说明 | §4.5 |
| 缺失项 6 | get_python_handler 缺失 | 🟢 | 不需修改 | Phase 5 不含 python 工具，当前路线图 §4.6 只列出 4 类内置工具，设计一致 | — |

### 未采纳项

无。所有阻塞级和重要级审计项均采纳。

### 部分采纳说明

**缝隙 5**：同意在 §4.2 增加警告注释，但不采纳"在 register_all() 中打印 needs_approval=True 的工具名到 info 日志"的建议。理由：该日志在每次启动时输出，属于噪声；审批标记属于代码审查职责，不应靠运行时日志补救。

### 文档变更摘要

- phase5-roadmap.md 版本从 v1.1 升级到 v1.2
- 新增：disabled_tools 字段、旧格式兼容代码、审批流程图、_cmd_tools() 修改、类型标注、验收编号修正
- 无文件新增/删除
