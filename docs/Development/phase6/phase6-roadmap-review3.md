# Phase 6 开发计划审计报告 v3

> 审计人：开发架构师（审计员3号）
> 审计日期：2026-06-03
> 审计对象：`docs/phase6/phase6-roadmap.md` 审计修正版 v2 — "MCP 协议集成"（审计员1号+2号修正后版本）
> 前置审计：
> - `docs/phase6/phase6-roadmap-review.md`（审计员1号：3 个阻塞级 + 4 个设计缝隙 + 3 个缺失项，已全部修正）
> - `docs/phase6/phase6-roadmap-review2.md`（审计员2号：1 个阻塞级 + 5 个设计缝隙，已全部修正）
> 代码基线：P5 已完成（ToolExecutor.get_definitions() / get_handler() 已就位，ToolSource.MCP 枚举已定义，mcp_enabled 字段已预留）

---

## 总体评价

**裁决：通过，可启动 Phase 6 开发。**

审计员1号和2号共计 16 项修正（4 个阻塞级 + 9 个设计缝隙 + 3 个缺失项）已在路线图当前版本中**全部正确落地**。经 P5 代码基线逐项验证——`ToolExecutor.get_definitions()`、`get_handler()`、`ToolSource.MCP` 枚举、`ToolsConfig.mcp_enabled` 全部就位——Phase 6 与 P5 预留扩展点的对齐程度是六个 Phase 中最高的，MCP 集成只需实现接口即插即入。

本轮审计未发现阻塞级问题。发现 **1 个代码示例缺陷**和 **3 个轻微改进建议**，修正成本极低。

---

## 一、前置审计修正验收

### 审计员1号（10 项修正）

| # | 审计项 | 级别 | 当前状态 | 验证结果 |
|---|--------|------|---------|---------|
| 缺陷 1 | MCP 配置双位置定义 | 🔴 | §3.1 统一在 `config/settings.py` | ✅ 正确落地 |
| 缺陷 2 | McpClient 缺少 read_resource/get_prompt | 🔴 | §3.2 补充两个方法 | ✅ 正确落地 |
| 缺陷 3 | Provider 访问 client._config | 🔴 | §3.2 新增 `get_tool_timeout()` | ✅ 正确落地 |
| 缝隙 4 | Resource/Prompt 命名冲突 | 🟡 | `read_{server}_{name}` / `prompt_{server}_{name}` | ✅ 正确落地 |
| 缝隙 5 | cast 模式类型不安全 | 🟡 | 三个独立 Handler 类 | ✅ 正确落地 |
| 缝隙 6 | 非文本 content 未处理 | 🟡 | `from_mcp()` / `from_resource_result()` / `from_prompt_result()` | ✅ 正确落地 |
| 缝隙 7 | /mcp 加载中状态不可见 | 🟡 | `_pending_servers` + `get_server_states()` 返回 STARTING | ✅ 正确落地 |
| 缺失项 3 | RESOURCE/PROMPT 测试覆盖 | 🟡 | §7 补充测试项 | ✅ 正确落地 |
| 缺失项 5 | get_handler() 是否存在 | 🟡 | P5 代码验证：executor.py:32 已实现 | ✅ 确认 |
| 缺失项 6 | __init__.py 导出 | 🟡 | §3.5 已补充 | ✅ 正确落地 |

### 审计员2号（6 项修正）

| # | 审计项 | 级别 | 当前状态 | 验证结果 |
|---|--------|------|---------|---------|
| 缺陷 1 | from_mcp() 类方法缺失 | 🔴 | §3.3 补充 McpResourceInfo.from_mcp() 和 McpPromptInfo.from_mcp() | ✅ 正确落地 |
| 缝隙 2 | MCP 工具审批不可控 | 🟡 | 三个 Handler 新增 `needs_approval` 参数；Provider 从 approval_commands 计算 | ✅ 正确落地 |
| 缝隙 3 | 三重超时嵌套 | 🟡 | §3.3 增加设计意图注释，保留 belt-and-suspenders | ✅ 正确落地 |
| 缝隙 4 | prompt 参数类型硬编码 | 🟡 | from_mcp() 保留 type；definition() 正确映射 type/required | ✅ 正确落地 |
| 缝隙 5 | `_cmd_tools()` 丢失审批标记 | 🟡 | MCP 工具列表也显示 `[需审批]` | ✅ 正确落地 |
| 缝隙 6 | ToolSource 导入缺失 | 🟡 | main.py 补充 import | ✅ 正确落地（见瑕疵 1）|

**结论**：16 项修正全部正确落地，零遗漏。

---

## 二、新发现问题

### 瑕疵 1：`ToolSource` 导入作用域问题（🟡 编码前修正）

**位置**：§3.6 main.py

**问题描述**：

P6 计划 §3.6 在 `_run_cli()` 函数**内部**放置了 `ToolSource` 导入：

```python
async def _run_cli():
    ...
    if config.tools.mcp_enabled and config.tools.mcp_servers:
        from dotclaw.mcp import MCPToolProvider
        from dotclaw.tools.base import ToolSource  # ← 函数级局部导入
        ...
```

但 `_cmd_tools()` 函数定义在模块级别（`_run_cli()` 外部），同样使用了 `ToolSource.BUILTIN` 和 `ToolSource.MCP`：

```python
def _cmd_tools(channel, tool_executor):
    ...
    builtin = [d for d in definitions if d.source == ToolSource.BUILTIN]   # NameError!
    mcp_tools = [d for d in definitions if d.source == ToolSource.MCP]     # NameError!
```

Python 中函数级 `from ... import` 的作用域仅限于该函数内部——模块级的 `_cmd_tools()` **无法访问** `_run_cli()` 中导入的 `ToolSource`。运行时 `_cmd_tools()` 调用会抛出 `NameError: name 'ToolSource' is not defined`。

**影响**：`/tools` 命令无法工作，用户无法查看工具列表。

**修正**：将 `from dotclaw.tools.base import ToolSource` 移到文件顶部的模块级导入区：

```python
# main.py 模块顶部
from dotclaw.tools.base import ToolSource
```

审计员2号缝隙 6 建议"补充 ToolSource 导入声明"，该修正已执行——但导入被放在了错误的作用域。将其移到模块级即可解决。

---

## 三、轻微改进建议（🟢 不阻塞）

### 建议 2：`McpResourceHandler` 和 `McpPromptHandler` 中的 `TimeoutError` 处理是死代码

**位置**：§3.3 `McpResourceHandler.execute()` 和 `McpPromptHandler.execute()`

**问题描述**：

两个 Handler 的 `execute()` 方法都包含：

```python
except asyncio.TimeoutError:
    return ToolResult(output="MCP 资源读取超时...", ...)
```

但在当前设计下，这个异常分支永远不会被触发：

| Handler | 内部超时来源 | 是否会触发内部 TimeoutError |
|---------|------------|--------------------------|
| McpToolCallHandler | `McpClient.call_tool()` 内含 `asyncio.wait_for` | ✅ 会触发 |
| McpResourceHandler | `McpClient.read_resource()` **无** `asyncio.wait_for` | ❌ 不会触发 |
| McpPromptHandler | `McpClient.get_prompt()` **无** `asyncio.wait_for` | ❌ 不会触发 |

ResourceHandler 和 PromptHandler 的超时完全依赖 ToolExecutor 外部 `asyncio.wait_for(handler.execute(), timeout)`——这个 `TimeoutError` 在 ToolExecutor 的 await 点抛出，不进 Handler 内部。

`except asyncio.TimeoutError` 在两个 Handler 中是**死代码**——永远不会执行到。实际超时行为正确（ToolExecutor 外层的 wait_for 会捕获），但死代码会误导后续维护者。

**建议**：以下二选一：

- **方案 A（推荐）**：为 Resource/Prompt Handler 也增加内部超时保护（与 ToolCallHandler 对齐）。在 `McpClient.read_resource()` 和 `McpClient.get_prompt()` 中增加 `asyncio.wait_for`：

```python
async def read_resource(self, uri: str) -> McpToolResult:
    ...
    try:
        result = await asyncio.wait_for(
            self._session.read_resource(uri),
            timeout=self.get_tool_timeout(),
        )
        return McpToolResult.from_resource_result(result)
    except asyncio.TimeoutError:
        raise  # 由 Handler 的统一 TimeoutError 处理
    except Exception as e:
        await self._handle_execution_error(e)
        raise
```

- **方案 B**：删除两个 Handler 中的 `except asyncio.TimeoutError` 分支，在注释中说明"超时由 ToolExecutor 外层 wait_for 统一控制"。保留 belt-and-suspenders 保护的一致性。

建议**方案 A**——让三个 Handler 的超时保护机制保持一致，且与审计员2号缝隙 3 的"三重超时嵌套"设计意图对齐。

---

### 建议 3：缺少 MCP Provider 关闭钩子

**位置**：§3.6 main.py

**问题描述**：

P6 计划 §3.6 展示了 MCP Provider 的初始化和 `/mcp` 命令，但**未展示退出时调用 `mcp_provider.shutdown()` 的逻辑**。在 `_run_cli()` 的退出路径（用户输入 `/quit` 或 Ctrl+C）中，需要确保：

- 所有 MCP server 收到 shutdown 通知
- stdio 子进程被正确终止（避免孤儿进程——§9.6 已标注此关注点）

**影响**：如果退出时不调用 shutdown，stdio server 子进程可能残留。

**建议**：在 §3.6 `_run_cli()` 的退出路径中补充：

```python
# /quit 命令处理
elif cmd == "/quit":
    if mcp_provider:
        await mcp_provider.shutdown()
    channel.print_info("再见！👋")
    break
```

并在异常退出路径（`except Exception` 或 `finally` 块）中也加入 shutdown 调用。

---

### 建议 4：`McpToolResult.from_mcp()` 中 `isError` 属性名确认

**位置**：§3.3 `McpToolResult.from_mcp()`

**问题描述**：

```python
is_error=mcp_result.isError if hasattr(mcp_result, 'isError') else False,
```

MCP SDK 的 `CallToolResult` 对象可能使用不同的属性名——Python SDK 通常将 MCP 协议的 `isError` 转换为 snake_case 的 `is_error`。`hasattr` 保护了不存在的属性名，但可能导致 `is_error` 永远为 `False`——即使 MCP server 返回了错误。

**建议**：修改为同时检查两种命名风格：

```python
if hasattr(mcp_result, 'isError'):
    is_error_val = mcp_result.isError
elif hasattr(mcp_result, 'is_error'):
    is_error_val = mcp_result.is_error
else:
    is_error_val = False
```

或者更简洁的：
```python
is_error_val = getattr(mcp_result, 'isError', None) or getattr(mcp_result, 'is_error', False)
```

§9.15 已标注"SDK 属性需确认"，本建议是对该标注的具体化。

---

## 四、P5 代码基线一致性验证

逐项验证 Phase 6 计划对 P5 状态的假设。

| 计划依赖的 P5 接口 | 代码基线验证 | 一致性 |
|-------------------|-------------|--------|
| `ToolExecutor.get_definitions()` | ✅ executor.py:28 — `return self._registry.get_definitions()` | 一致 |
| `ToolExecutor.get_handler(name)` | ✅ executor.py:32 — `return self._registry.get(name)` | 一致 |
| `ToolSource.MCP` 枚举值 | ✅ base.py:16 — `MCP = "mcp"` | 一致 |
| `ToolsConfig.mcp_enabled` | ✅ settings.py:69 — `mcp_enabled: bool = True` | 一致 |
| `ToolProvider` ABC 存在 | ✅ tools/provider.py — `discover_and_register(registry: "ToolRegistry")` | 一致 |
| `ToolHandler` ABC 存在 | ✅ tools/handler.py — `definition()` + `execute()` | 一致 |
| `ToolDefinition.source` 字段 | ✅ base.py — `source: ToolSource = ToolSource.BUILTIN` | 一致 |
| `config/settings.py` `_expand_env()` | ✅ 在 `_raw_to_config()` 流程中统一展开 `${VAR}` | 一致 |
| `ToolRegistry.register(handler)` | ✅ registry.py — 同名覆盖 | 一致 |

**结论**：Phase 6 对 P5 的所有接口依赖均已就位，无一遗漏。

---

## 五、长期发展性评估

在审计员1号和2号的前瞻性评估基础上，补充以下视角。

| 维度 | 评分 | 说明 |
|------|------|------|
| **P7 Skill 工具化** | ✅ 好 | `ToolProvider` ABC + `ToolSource.SKILL` 枚举已预留，Skill 集成模式与 MCP 一致 |
| **多 MCP server 并行** | ✅ 好 | `asyncio.gather` 并行连接 + 独立状态管理 + server 前缀命名 |
| **MCP 协议版本演进** | ✅ 好 | 使用官方 `mcp` SDK，SDK 升级即可兼容新版本 |
| **审批机制统一性** | ✅ 好 | 审计员2号缝隙 2 修正后，MCP 工具审批与内置工具使用相同的 AND 双门机制 |
| **重连策略** | ✅ 好 | `max_restart_attempts=3` + per-server 可覆盖，合理 |
| **类型安全** | ✅ 好 | 三个独立 Handler 类消除 cast 模式，各自持有确定类型的 info |
| **非文本 content 降级** | ✅ 好 | 三级降级（from_mcp / from_resource_result / from_prompt_result）覆盖所有 content 类型 |
| **可观测性** | ✅ 好 | `/mcp` 三态可见（starting/connected/failed）、`/tools` 按来源分组 |
| **MCP content 非文本类型处理** | ✅ 好 | image/resource content 降级为描述文本 |
| **跨 server 命名冲突** | ✅ 好 | Tool 原名覆盖（设计如此），Resource/Prompt server 前缀防冲突 |
| **子进程清理** | ⚠️ 关注 | §9.6 已标注为实施时关注点——依赖 SDK 的 shutdown 行为 |

### 审计员2号长期关注追踪

| # | 建议 | P6 处理 | 后续 |
|---|------|--------|------|
| Tool 同名覆盖日志 | 跨 server 工具名冲突排查 | §9.8 "输出 info 日志" | ✅ 已融入 |
| SDK 属性名确认 | `isError`、`mimeType`、`inputSchema` 等 | §9.15 "SDK 属性确认" | ✅ 已标注 |

---

## 六、改进建议汇总

### 编码前修正

| # | 建议 | 级别 | 影响位置 |
|---|------|------|---------|
| 1 | `ToolSource` 导入从 `_run_cli()` 函数级移到模块级 | 🟡 | §3.6 main.py |

### 轻微改进（按需采纳）

| # | 建议 | 级别 | 影响位置 |
|---|------|------|---------|
| 2 | Resource/Prompt Handler 补充内部超时（与 ToolCallHandler 对齐），或删除死代码 TimeoutError 分支 | 🟢 | §3.3 |
| 3 | main.py 补充 MCP Provider shutdown 钩子 | 🟢 | §3.6 |
| 4 | `isError` 属性名同时检查 camelCase 和 snake_case | 🟢 | §3.3 |

---

## 七、结论

**裁决：通过，可启动 Phase 6 开发。**

Phase 6 审计修正版计划经三轮审计，共计发现并修正 **17 项问题**（4 个阻塞级 + 10 个设计缝隙/缺失 + 3 个轻微建议），所有阻塞级问题已在当前版本中解决。

本轮审计（v3）发现的 **1 个代码示例缺陷**（ToolSource 导入作用域）修正成本微不足道——将一行 import 从函数级移到模块级即可。3 个轻微建议不影响功能正确性，按需采纳。

**架构评估**：
- 配置集中化（`config/settings.py`）与 P4 MemoryConfig 模式一致，避免了循环依赖
- 三个独立 Handler 类设计消除了类型不安全的 cast 模式
- MCP 工具审批机制与内置工具完全一致（AND 双门），修正了"审批不可控"的设计缺陷
- 非文本 content 三级降级处理覆盖了 MCP 协议的全部三种 content 类型
- `/mcp` 三态可见 + `/tools` 按来源分组，运维可观测性到位
- 双传输支持（stdio + Streamable HTTP）覆盖本地和远程 MCP server
- 后台异步加载不阻塞 Agent 启动，用户体验设计合理
- 与 P5 预留接口的对齐程度是六个 Phase 中最高的——所有扩展点均已就位

**给开发人员的行动项**：
1. 将 `from dotclaw.tools.base import ToolSource` 移到 main.py 模块顶部
2. 轻微建议 2-4 按需采纳，不阻塞开发

**Phase 6 开发可以启动。**

---

*文档版本：v3.0*
*审计日期：2026-06-03*
*审计人：审计员3号*
