# dotClaw 项目架构分析与开发路线图

更新时间：2026-05-29（含 CowAgent 对比分析）

## 项目定位

dotClaw 是一个轻量级 AI Agent 框架，定位是"学习 AI 应用开发"，
采用 Python 3.13+ / asyncio 异步架构，以 OpenAI 兼容接口调用千问（Qwen）等 LLM。

## 当前架构（模块关系）

```
dotClaw/
├── main.py            ← 入口：组装各组件，启动 CLI 主循环
├── config/settings.py ← 配置层：YAML → dataclass，支持 ${ENV} 展开
├── llm/
│   ├── base.py        ← 抽象层：Message / ToolCall / ChatChunk / LLMClient(ABC)
│   ├── qwen.py        ← 实现层：AsyncOpenAI SDK 调用千问 API
│   └── proxy.py       ← 代理层：重试 + 降级 + 统一异步迭代器接口
├── agent/loop.py      ← 核心循环：完整 ReAct 循环（Phase 1 ✅）
├── channel/
│   ├── base.py        ← 抽象层：receive / send / stream / ask_user / print_*
│   └── cli.py         ← 实现层：rich 美化 + asyncio Executor 包装 input/print
├── memory/store.py    ← 会话持久化：每个 Session = 一个 JSON 文件
├── tools/
│   ├── __init__.py    ← 自动注册所有工具模块
│   ├── base.py        ← 注册表：@register_tool 装饰器 + ToolRegistry
│   ├── approval.py    ← 危险工具审批（exec/python 需用户确认）
│   ├── exec_tool.py   ← Shell 执行工具
│   ├── file_tool.py   ← read_file / write_file / list_dir
│   ├── memory_tool.py ← memory_read / memory_write（长期记忆）
│   └── system_tool.py ← system_info / get_time
├── skills/loader.py   ← 加载 skills/*/SKILL.md，解析 YAML frontmatter
├── scheduler/reminder.py ← 最简异步定时提醒
├── debug/logger.py   ← TraceRecord 推理追踪 + Python logging
└── tests/
    └── test_phase1_acceptance.py ← Phase 1 验收测试（5 场景 + 2 回归）
```

## 当前实现状态

| 模块 | 状态 | 说明 |
|------|------|------|
| 配置系统 | ✅ 完整 | YAML → dataclass，环境变量展开，单例模式 |
| LLM 抽象层 | ✅ 完整 | Message 含 tool_calls 字段，ChatChunk 流式设计 |
| Qwen 客户端 | ✅ 完整 | 流式 tool_calls 增量拼接已修复；_convert_messages 序列化完整 |
| LLM Proxy | ✅ 完整 | 重试 + 降级逻辑正确 |
| Agent 循环 | ✅ 完成 | 完整 ReAct 循环（LLM → 工具 → 循环）；10 轮上限；流式输出 |
| CLI Channel | ✅ 完整 | rich 渲染，流式输出，print_* 方法完整 |
| 会话管理 | ✅ 完整 | JSON 文件存储，多会话，CRUD 完整；load() 返回对象（非 dict） |
| 工具系统 | ✅ 完整 | 8 个工具自动注册；审批机制从 config 读取；__init__.py 触发导入 |
| DebugManager | ✅ 完成 | TraceRecord 记录；/debug 命令可用 |
| 测试 | ✅ 已写 | Phase 1 5 个场景 + 2 个回归验证全部通过 |
| Skill 加载 | ⚠️ 半成品 | 能加载 SKILL.md，但未注入 system prompt |
| 定时提醒 | ⚠️ 最简版 | 仅一次性延迟提醒，无 cron |
| 长期记忆 | ⚠️ 半成品 | 工具有了，但 Agent 循环未调用 |
| 上下文管理 | ❌ 未实现 | max_context_tokens 未使用 |
| python 工具 | ❌ 未注册 | approval.py 引用了但代码中未找到注册 |

---

## 与 CowAgent 的对比分析

dotClaw 参考项目 **CowAgent**（位于 `D:\dev\CowAgent`）是一个成熟的、**生产级的 AI Agent 支撑框架**（Agent Harness），支持 16+ LLM 供应商、9 种 IM 通道、完整的三级记忆系统 + Deep Dream 蒸馏等。dotClaw 定位为**轻量级学习框架**，两者定位存在根本差异：

| 维度 | dotClaw（学习框架） | CowAgent（生产级框架） |
|------|---------------------|------------------------|
| **定位** | 学习 AI Agent 开发 | 超强 AI 助手（Super AI Assistant） |
| **代码规模** | ~30 个 Python 文件 | ~200+ 个 Python 文件 |
| **LLM 支持** | Qwen（可扩展） | 16+ 供应商，OpenAICompatibleBot 混入模式 |
| **通道** | CLI（可扩展） | 9 种：Web、微信、飞书、钉钉、企微、QQ 等 |
| **记忆系统** | 单文件 MEMORY.md | 三级架构：上下文 → 日记忆 → MEMORY.md + Deep Dream 蒸馏 |
| **上下文管理** | 未实现 | 分层压缩策略 + LLM 摘要注入 + 溢出恢复 |
| **工具系统** | 8 个基础工具 | 17 个工具 + MCP 协议集成 + 条件加载 |
| **工具健壮性** | 基础重试 | 连续失败检测、无限循环保护、JSON 修复 |
| **Skill 系统** | 加载 + 未注入 | Skill Hub 生态 + 对话式创建器 |
| **取消机制** | 无 | CancelTokenRegistry + 安全检查点 |
| **配置系统** | YAML → dataclass | JSON + 环境变量覆盖 + 85 配置项 |
| **通道模式** | 同步输入/输出 | 生产者-消费者 + 线程池 |
| **部署** | pip install -e . | Docker + 多平台脚本 |

### dotClaw 可借鉴的设计（按优先级排序）

以下 CowAgent 的核心设计对 dotClaw 有直接参考价值，已融入下方更新后的开发路线图：

1. **三级记忆 + Deep Dream 蒸馏**：上下文 → 日记忆 → MEMORY.md，夜间 LLM 自动蒸馏 → Phase 2
2. **上下文分层压缩策略**：根据消息类型和轮次动态截断，工具结果分级截断 → Phase 2
3. **工具失败恢复机制**：相同参数连续失败/循环检测、JSON 参数修复 → Phase 4
4. **条件性工具加载**：根据环境变量/依赖自动决定工具是否可用 → Phase 4
5. **OpenAICompatibleBot 混入模式**：一个基类支持所有 OpenAI 兼容 LLM → Phase 5
6. **MCP 协议集成**：动态工具注册 + 增量热重载 → Phase 6
7. **Cancel Token Registry**：请求级/会话级取消 + 安全检查点 → Phase 7
8. **Skill 创建工作流**：对话式引导创建 SKILL.md → Phase 3

### dotClaw 不需要借鉴的部分

这些 CowAgent 功能超出 dotClaw 作为学习框架的定位：

- **16+ LLM 供应商**：dotClaw 支持 3-5 个关键供应商即可（Qwen + DeepSeek + OpenAI）
- **9 种 IM 平台**：dotClaw 只需 CLI + 可选 Web UI
- **TTS/语音/翻译**：非 Agent 框架核心功能
- **Docker/K8s 部署**：学习框架不需要容器化
- **插件系统**：过于复杂，Skill 系统可替代
- **Web 控制台**：远期可选

---

## 开发路线图（更新版）

### Phase 1：让 Agent 跑起来（已完成 ✅）

**目标**：实现完整 ReAct 循环，让框架真正能跑起来。

**完成日期**：2026-05-29

**实现内容**：
- ✅ 实现 `AgentLoop.run()` 完整 ReAct 循环（LLM 调用 → 工具调用 → 循环）
- ✅ 修复 `qwen.py` 中 `_parse_stream_chunk` 的 arguments 增量拼接 bug
- ✅ 接入 `DebugManager`，每次 `run()` 记录 `TraceRecord`
- ✅ 实现流式输出（`AgentLoop` → `Channel.stream()`）
- ✅ 更新 `main.py`，初始化并传入 `ToolRegistry` 和 `ApprovalManager`
- ✅ 修复 `ToolRegistry.execute()` 中 `needs_approval` 从 config 读取
- ✅ `Channel` 基类添加 `print_error()` / `print_info()` / `print_markdown()` 默认实现
- ✅ `Message` 新增 `tool_calls` 字段；`_convert_messages` 序列化为 OpenAI 格式
- ✅ `AgentLoop` 执行工具前插入 `assistant(tool_calls)` 消息
- ✅ `SessionManager.load()` 返回 `SessionMessage` 对象（非 dict）
- ✅ 创建 `tools/__init__.py` 自动注册所有工具模块
- ✅ CLI 新增 `/tools` 命令和工具调用通知
- ✅ 屏蔽 httpx/openai 的 INFO 日志避免 CLI 污染

**验收标准**：
1. ✅ 纯文本对话：输入问题，LLM 返回文本回复，流式输出
2. ✅ 带工具调用的对话：输入需要工具的问题，正确执行工具并返回结果
3. ✅ 危险工具审批：执行 `exec` 工具时请求用户确认
4. ✅ 调试追踪：输入 `/debug` 显示最近一次推理过程
5. ✅ 多轮对话：LLM 能记住之前说的话

**测试**：`tests/test_phase1_acceptance.py` — 5 个场景 + 2 个回归验证全部通过。

**修改的文件**：

| 文件 | 修改内容 |
|------|----------|
| `src/dotclaw/llm/base.py` | Message 新增 `tool_calls` 字段 |
| `src/dotclaw/llm/qwen.py` | 修复流式 args 拼接；`_convert_messages` 序列化 tool_calls |
| `src/dotclaw/agent/loop.py` | 完整 ReAct 循环；assistant(tool_calls) 消息；工具调用通知 |
| `src/dotclaw/main.py` | 初始化 ToolRegistry/ApprovalManager；`/tools` 命令；日志屏蔽 |
| `src/dotclaw/tools/base.py` | `execute()` 从 config 读取 needs_approval |
| `src/dotclaw/tools/__init__.py` | 新建，自动注册所有工具模块 |
| `src/dotclaw/channel/base.py` | 添加 print_error/info/markdown 默认实现 |
| `src/dotclaw/channel/cli.py` | 去掉 Console.print 不支持的 flush 参数 |
| `src/dotclaw/memory/store.py` | `_dict_to_session()` 确保 load 返回 SessionMessage 对象 |
| `tests/test_phase1_acceptance.py` | Phase 1 验收测试（MockLLM + FakeChannel） |

---

### Phase 2：上下文压缩与记忆蒸馏（设计完善中）

> **CowAgent 参考**：三级记忆系统 + 分层压缩策略 + Deep Dream 蒸馏

**目标**：让 Agent 拥有持久记忆和智能化的上下文管理能力。

**记忆系统**（三级架构，参考 CowAgent）：
1. **短期**：Session 内完整的 messages 列表（已完成）
2. **中期**：每日对话摘要（`memory/YYYY-MM-DD.md`），自动生成 + 追加
3. **长期**：`data/memory/MEMORY.md`，夜间 LLM 蒸馏（Deep Dream 模式）

**上下文压缩策略**（参考 CowAgent 的分层截断）：
- 工具结果分级截断：当前轮 50K → 历史轮 20K → 溢出恢复 10K
- 被裁剪的消息通过 LLM 摘要后注入回上下文
- 确保 tool_use / tool_result 配对不被破坏

**Deep Dream 灵感功能**（dotClaw 简化版）：
- **触发时机**：每日 23:55 或手动 `/dream` 命令
- **蒸馏流程**：扫描当日所有 `memory/YYYY-MM-DD.md` → LLM 提取关键信息 → 更新 MEMORY.md
- **去重保护**：基于内容哈希避免重复写入
- **梦境日记**：记录蒸馏过程摘要（可选）

**实现要点**：
- `AgentLoop._build_messages()` 中：Phase 1 加载全部历史 → Phase 2 根据 max_context_tokens 截断
- `AgentLoop._build_messages()` 中：MEMORY.md 内容注入 system prompt（长期记忆）
- 新增 `MemoryFlushManager`：管理日记忆刷新 + Deep Dream 触发
- `SessionManager` 可能需要升级存储（JSON → SQLite？取决于数据量需求）

---

### Phase 3：Skill 系统完善

> **CowAgent 参考**：Skill 注入 + 对话式创建器 + 条件过滤

**目标**：让 Skill 成为 Agent 可用的能力扩展机制。

**Phase 3 内容**：
- 将 Skill 描述注入 system prompt（`_build_messages()` 中调用 `SkillLoader.build_skill_prompt()`）
- 支持 Skill 中声明的脚本执行（SKILL.md 的 instructions 可供 LLM 通过工具执行）
- 支持热加载（运行时新增 SKILL.md 无需重启）
- **新增**：Skill 创建引导（简单的对话式流程，参考 CowAgent 的 skill-creator）
- **新增**：Skill 条件过滤（根据平台/依赖/环境变量判断是否可用）

**与 CowAgent 的差异**：
- CowAgent 有完整的 Skill Hub 生态和远程安装；dotClaw 先实现本地技能管理
- CowAgent 的 Skill 是可执行的 Markdown 文件；dotClaw 的 Skill 是 Agent 通过工具调用的指令集
- dotClaw 不需要前端的 Skill Web API，本地 CLI 即可

---

### Phase 4：工具系统增强

> **CowAgent 参考**：条件性工具加载 + 失败恢复机制 + web_search/web_fetch

**目标**：增强工具系统健壮性和功能性。

**新增工具**：
- `python` — 执行 Python 代码片段（需审批，类似 exec 的审批机制）
- `web_search` — 网络搜索（需 API key，条件加载）
- `web_fetch` — 获取 URL 内容并提取文本

**条件性工具加载**（参考 CowAgent）：
- 工具根据环境变量/API Key 的可用性条件性加载
- 启动时自动检测：无 API key → 工具不注册，Agent 看不到
- `web_search` 没有 API key 时自动隐藏
- 热重载支持：环境变量变化后自动刷新可用工具

**工具失败恢复机制**（参考 CowAgent）：
- 相同参数连续失败检测（N 次 → 停止该工具调用）
- 无限循环检测（相同参数连续调用 M 次 → 提示 LLM 换策略）
- JSON 参数修复：解析失败时尝试常见修复（引号、结尾逗号等）

---

### Phase 5：多模型支持

> **CowAgent 参考**：OpenAICompatibleBot 混入模式 + 模型自动检测

**目标**：支持多个 LLM 供应商，统一 OpenAI 兼容接口。

**架构设计**：
- 新增 `OpenAICompatibleClient` 基类：封装 OpenAI 兼容 API 的通用逻辑（tool_calls 流式解析、消息格式转换）
- `QwenClient` 继承 `OpenAICompatibleClient`，只覆写 base_url 和 model
- 新增 `DeepSeekClient`、`OpenAIClient` 等
- 模型自动检测：根据 model 名称前缀推断供应商 → 自动选择对应 Client
- 模型切换命令：`/model <name>` 支持热切换（已完成，需扩展多供应商）

**与 CowAgent 的差异**：
- CowAgent 支持 16+ 供应商，dotClaw 支持 3-5 个：Qwen、DeepSeek、OpenAI（可扩展到 Claude、Gemini）
- CowAgent 需要 Claude ↔ OpenAI 格式互转；dotClaw 只处理 OpenAI 兼容格式（更简洁）
- CowAgent 的 thinking/reasoning_effort 透传；dotClaw Phase 5 可选实现

---

### Phase 6：MCP 协议集成

> **CowAgent 参考**：MCP 客户端 + 动态工具注册 + 增量热重载

**目标**：通过 MCP（Model Context Protocol）协议动态注册外部工具。

**Phase 6 内容**：
- MCP 客户端实现：支持 stdio 和 SSE 传输
- 配置文件：`config.yaml` 中声明 `mcp_servers` 列表
- 动态工具注册：MCP 服务器提供的工具自动注册到 `ToolRegistry`
- 增量热重载：监测配置文件变化，差异式更新工具列表（参考 CowAgent）
- 服务器隔离：单个 MCP 服务器崩溃不影响其他服务器和主流程

**与 CowAgent 的差异**：
- 少了一些高级功能，但核心能力一致
- dotClaw 使用 YAML 配置而非 CowAgent 的 JSON，保持一致性

---

### Phase 7：Scheduler 增强 + 取消机制

> **CowAgent 参考**：CancelTokenRegistry + 安全检查点

**目标**：增强定时任务能力，增加请求取消支持。

**Scheduler 增强**：
- 支持 cron 表达式（而非仅一次性延迟提醒）
- 定时任务触发 Agent 处理（调用 `AgentLoop.run()`）
- 持久化定时任务（重启不丢失）

**取消机制**（参考 CowAgent）：
- `CancelTokenRegistry`：进程内请求取消注册表
- 支持按 `request_id` 或 `session_id` 取消
- Agent 循环安全检查点：轮次边界、工具执行前后、LLM 流式块之间
- CLI `/cancel` 命令：取消当前正在执行的 Agent 请求

---

### Phase 8：测试 稳定性 多渠道（远期）

> **测试与稳定性**（参考 CowAgent 的测试结构）
- pytest 单元测试覆盖核心模块
- 集成测试：多轮对话 + 工具调用完整流程
- Mock LLM 客户端支持（已完成基础版本）
- 边界情况处理：API 限流、网络异常、JSON 解析失败等

> **多渠道接入**（参考 CowAgent 的 Channel 系统）
- Web UI Channel（SSE 流式）
- 可能的 IM Channel（取决于需求）
- dotClaw 作为学习框架，多渠道优先级不高
