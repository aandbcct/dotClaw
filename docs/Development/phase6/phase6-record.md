# dotClaw Phase 6 MCP 协议集成 — 开发日志

> 本文件记录 P6 MCP 协议集成的开发进度、变更记录。
> 架构文档见 `docs/phase6/phase6-roadmap.md`。

## 变更日志

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| v1.0 | 2026-06-03 | Phase 6 MCP 协议集成首次实施，配置层+客户端+适配层+Provider+集成全部完成，95/95 测试通过 |
| v1.1 | 2026-06-03 | 修复代码审查 W1（重连时 transport 泄漏）+ W2（client 状态泄漏）+ M1-M5（timeout/引用/校验/context/类型标注），95/95 测试通过 |

### 变更内容

- 新增 `pyproject.toml`：添加 `mcp>=1.0.0` 依赖
- 新增 `config/settings.py`：McpGlobalConfig / McpServerConfig dataclass + ToolsConfig 扩展 mcp_global/mcp_servers 字段 + _parse_mcp_global/_parse_mcp_servers 解析函数（含 transport/name/command/url 校验 + 环境变量展开）
- 修改 `config.yaml`：新增 mcp_global 全局配置段 + mcp_servers 列表（含示例注释）
- 新增 `mcp/__init__.py`：包入口，导出 McpClient / McpClientState / Handler 三件套 / Info 数据类 / MCPToolProvider
- 新增 `mcp/client.py`：McpClient 类（封装 mcp SDK，stdio/streamable_http 双传输）+ McpClientState 状态机 + McpError/McpClientError/McpUnavailableError 异常层次 + McpToolInfo/McpResourceInfo/McpPromptInfo/McpToolResult 数据类
- 新增 `mcp/tool_adapter.py`：McpToolCallHandler（tools/call）+ McpResourceHandler（resources/read，命名 read_{server}_{name}）+ McpPromptHandler（prompts/get，命名 prompt_{server}_{name}），全部实现 ToolHandler ABC
- 新增 `mcp/provider.py`：MCPToolProvider（ToolProvider ABC 实现）— 编排 servers 并行连接 + 工具注册 + 三层状态管理（clients/pending/failed）+ shutdown
- 修改 `main.py`：MCP 初始化链（后台 asyncio.create_task 加载）+ /mcp 命令（显示连接状态含图标）+ /tools 命令增强（按 ToolSource.BUILTIN/MCP 分组显示，MCP 按 server 二次分组）+ 帮助文本新增 /mcp
- 新增 `tests/test_phase6_acceptance.py`：28 tests / 7 场景（配置解析 / 状态机 / Info-Result / Handler 定义 / Provider / 错误类型 / 回归）

### 回归测试结果

| 测试套件 | 测试数 | 通过 | 状态 |
|----------|--------|------|------|
| Phase 1 验收 | 7 | 7 | ✅ |
| Phase 2 验收 | 7 | 7 | ✅ |
| Phase 3 验收 | 8 | 8 | ✅ |
| Phase 4 验收 | 6 | 6 | ✅ |
| Phase 5 验收 | 39 | 39 | ✅ |
| Phase 6 验收 | 28 | 28 | ✅ |
| **合计** | **95** | **95** | **✅** |

---

## v1.1 — 2026-06-03

### 变更内容

根据代码审查报告 `docs/phase6/phase6-codeReview.md` 修复 Warning 级别问题 W1/W2 + Minor 级别问题 M1-M5。

### 已修复（来自审查 Warning + Minor）

| # | 原问题 | 修复内容 | 涉及文件 |
|---|--------|----------|----------|
| ✅ W1 | 重连时旧 transport 未清理 | `connect()` 开头调用 `_cleanup_old_connection()` — 先 shutdown session → terminate stdio transport（hasattr 防护）→ 置 None；`shutdown()` 同样增加 transport cleanup | `mcp/client.py` |
| ✅ W2 | `_connect_and_register` 异常时 client 状态泄漏 | 将 `self._clients[client.server_name] = client` 从方法开头移到全部 tools/resources/prompts 注册成功后，防止注册中断时 client 残留在 clients 字典中 | `mcp/provider.py` |
| ✅ M1 | `read_resource()` / `get_prompt()` 无 timeout | 添加 `timeout` 参数（None=使用全局默认），内部 `asyncio.wait_for` 控制超时 + 超时时 `_send_cancel()` | `mcp/client.py` |
| ✅ M2 | MCP 后台 task 引用未保存 | 保存 `mcp_task = asyncio.create_task(...)` 引用；`/quit` 退出时 `cancel()` + `await` + `provider.shutdown()` | `main.py` |
| ✅ M3 | `McpServerConfig` 默认值允许无效状态 | 新增 `__post_init__` 校验：`name` 不能为空 + `transport` 必须合法 | `config/settings.py` |
| ✅ M4 | Handler `execute()` 的 `context` 参数未使用 | 三个 Handler 的 `execute()` 新增 `context.timeout` fallback 逻辑 | `mcp/tool_adapter.py` |
| ✅ M5 | `from_mcp()` 方法签名缺少类型标注 | 新增 `TYPE_CHECKING` 类型导入 + 6 个 `from_mcp` 参数标注 | `mcp/client.py` |

---

*本文件由 dotClaw 开发工程师维护。Phase 6 审查修复完成。*
