# dotClaw Phase 5 工具层架构重构 — 开发日志

> 本文件记录 P5 工具层架构重构的开发进度、变更记录。
> 架构文档见 `docs/phase5-roadmap.md`。

## 变更日志

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| v1.0 | 2026-06-03 | Phase 5 工具层架构重构首次实施，14步全部完成，63/63 测试通过 |
| v1.1 | 2026-06-03 | 修复代码审查 W1（exec_command 孤儿子进程）+ W2（read_file 文件大小限制），67/67 测试通过 |

### 变更内容

- 重构 `tools/base.py`：ToolDefinition 扩展（source/needs_approval/timeout/metadata）、ToolResult 结构化扩展（error_code/error_type/metadata）、新增 ToolExecutionContext，删除全局 `_registry` 和 `register_tool` 装饰器
- 新增 `tools/handler.py`：ToolHandler ABC 统一接口 + BuiltinToolHandler 适配器
- 新增 `tools/registry.py`：ToolRegistry 纯注册表（register/get/list/unregister/clear），同名静默覆盖
- 新增 `tools/executor.py`：ToolExecutor 调度层（审批检查 + asyncio.wait_for 超时控制 + 结构化错误处理）
- 重构 `tools/approval.py`：删除硬编码 NEEDS_APPROVAL，新增 _approval_commands 集合从 config 加载
- 新增 `tools/builtin/` 子包：迁移 exec/file/memory/system 工具为 BuiltinToolHandler 工厂函数，统一通过 `register_all()` 注册 8 个内置工具
- 新增 `tools/provider.py`：ToolProvider ABC 骨架，为 Phase 6 MCP / Phase 7 Skill 预留
- 合并 `debug/logger.py`（TraceRecord + DebugManager）到 `agent/logger.py`，AgentLogger 直接管理 _last_trace + _setup_logging，消除跨模块依赖
- 修改 `agent/loop.py`：`_tool_registry` → `_tool_executor`，删除 `_debug_manager`，`debug_trace()` 改为从 `_logger` 获取 trace
- 修改 `main.py`：组装新架构 ToolRegistry → register_all() → ApprovalManager → ToolExecutor → AgentLoop，删除 DebugManager 引用
- 修改 `config/settings.py`：ToolsConfig 扩展（source 级启停 + approval_commands + disabled_tools + exec_timeout），向后兼容旧 per-tool 格式
- 修改 `config.yaml`：tools 段结构更新为标准格式
- 删除旧文件：`tools/exec_tool.py`, `tools/file_tool.py`, `tools/memory_tool.py`, `tools/system_tool.py`（已迁移到 builtin/）
- 删除 `debug/` 子包整体（TraceRecord + DebugManager 已合并到 agent/logger.py）
- 更新 `tools/__init__.py`：导出新架构模块

### 回归测试结果

| 测试套件 | 测试数 | 通过 | 状态 |
|----------|--------|------|------|
| Phase 1 验收 | 7 | 7 | ✅ |
| Phase 2 验收 | 7 | 7 | ✅ |
| Phase 3 验收 | 8 | 8 | ✅ |
| Phase 4 验收 | 6 | 6 | ✅ |
| Phase 5 验收 | 39 | 39 | ✅ |
| **合计** | **67** | **67** | **✅** |

---

## v1.1 — 2026-06-03

### 变更内容

根据代码审查报告 `docs/phase5-codeReview.md` 修复 Warning 级别问题 W1 和 W2。

### 已修复（来自审查 Warning）

| # | 原问题 | 修复内容 | 涉及文件 |
|---|--------|----------|----------|
| ✅ W1 | exec_command 双层超时导致孤儿子进程 | 添加 `CancelledError` 处理分支：ToolExecutor `asyncio.wait_for` 超时 cancel task 时，`CancelledError`（继承 `BaseException`）不被 `except Exception` 捕获 → 新增独立 `except CancelledError` 块确保 `proc.kill()` + `await proc.wait()` 执行后再 `raise` | `tools/builtin/exec_tool.py` |
| ✅ W2 | `read_file` 无文件大小限制 | 新增 `MAX_FILE_SIZE = 10 * 1024 * 1024` 常量 + `file_path.stat().st_size > MAX_FILE_SIZE` 检查，超大文件返回错误提示 | `tools/builtin/file_tool.py` |

### 新增测试

| # | 测试场景 | 验证内容 |
|---|---------|---------|
| 场景 14 | W1 修复验证 | `exec_command` 源码包含 `CancelledError` 处理 + `proc.kill()` 调用；正常执行 `echo hello` 不受影响 |
| 场景 15 | W2 修复验证 | 小文件正常读取；超过 10MB 文件返回"文件过大"错误 |

---

*本文件由 dotClaw 开发工程师维护。Phase 5 重构完成。*
