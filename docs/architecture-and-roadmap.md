# dotClaw 项目架构分析与开发路线图

更新时间：2026-06-03（Phase 6 完成，含 CowAgent 对比分析）

## 项目定位

dotClaw 是一个轻量级 AI Agent 框架，定位是"学习 AI 应用开发"，
采用 Python 3.13+ / asyncio 异步架构，以 OpenAI 兼容接口调用千问（Qwen）等 LLM。

## 目标架构（含规划中的模块）

```
dotClaw/
├── main.py              ← 入口：组装各组件，启动 CLI 主循环
├── config/settings.py   ← 配置层：YAML → dataclass，支持 ${ENV} 展开
│
├── llm/                 ✅ 已完成
│   ├── base.py          ← 抽象层：Message / ToolCall / ChatChunk / LLMClient(ABC)
│   ├── openai_compat.py ← OpenAICompatibleClient 通用基类（Phase 2 ✅）
│   ├── qwen.py          ← QwenClient（继承 OpenAICompatibleClient）
│   ├── deepseek.py      ← DeepSeekClient（Phase 2 ✅）
│   ├── openai.py        ← OpenAIClient（Phase 2 ✅）
│   ├── model_router.py  ← 模型路由器：priority 确定选择 + 降级链（Phase 2 ✅）
│   └── proxy.py         ← 代理层：多供应商路由 + RateLimiter + 降级
│
├── agent/               ✅ 已完成（Phase 3）
│   ├── loop.py          ✅ 核心循环：ReAct + AgentContext + AgentResult + purpose
│   ├── context.py       ← AgentContext 不可变快照（Phase 3 ✅）
│   ├── result.py        ← AgentResult 标准化返回类型（Phase 3 ✅）
│   ├── message_utils.py ← 消息验证/裁剪/清理（Phase 3 ✅）
│   ├── logger.py        ← AgentLogger + TraceRecord（Phase 5 合并 DebugManager）（Phase 3 ✅ → Phase 5 修改）
│   └── prompt/
│       ├── builder.py   ← PromptBuilder 模块化构建器（Phase 3 ✅）
│       └── providers.py ← DataProvider + Role/Rules/Tools Provider（Phase 3 ✅）
│
├── channel/             ✅ 已完成
│   ├── base.py
│   └── cli.py
│
├── common/              ✅ 已完成（Phase 2）
│   ├── rate_limiter.py  ← 令牌桶限流器（asyncio.Lock 并发安全）
│   ├── singleton.py     ← SingletonMeta 元类
│   └── utils.py         ← expand_env_vars（未解析 WARNING）/ safe_load_yaml
│
├── memory/              ✅ 已完成（Phase 4）
│   ├── store.py         ✅ 会话持久化（JSON，load 返回 SessionMessage）
│   ├── storage.py       ← SQLite + FTS5 双索引 + 向量检索（Phase 4 ✅）
│   ├── chunker.py       ← Markdown 结构感知文本分块（Phase 4 ✅）
│   ├── embedding.py     ← EmbeddingProvider + LRU Cache（Phase 4 ✅）
│   ├── manager.py       ← MemoryManager 核心调度：混合检索 + sync（Phase 4 ✅）
│   ├── flush.py         ← MemoryFlushManager：L2 日记忆写入 + LLM 摘要（Phase 4 ✅）
│   └── dream.py         ← DeepDream：L3 蒸馏 + MEMORY.md 备份（Phase 4 ✅）
│
├── tools/               ✅ 已完成（Phase 5 架构重构）
│   ├── __init__.py      ← 导出 ToolRegistry, ToolExecutor, ToolProvider
│   ├── base.py          ← ToolDefinition + ToolResult + ToolExecutionContext（重构）
│   ├── handler.py       ← ToolHandler ABC + BuiltinToolHandler（Phase 5）
│   ├── registry.py      ← ToolRegistry 纯注册表（Phase 5）
│   ├── executor.py      ← ToolExecutor 调度+审批+超时（Phase 5）
│   ├── approval.py      ← ApprovalManager（重构，删除硬编码）
│   ├── provider.py      ← ToolProvider ABC 骨架（Phase 5，MCP/Skill 预留）
│   └── builtin/         ← 内置工具子包（Phase 5 迁移）
│       ├── __init__.py  ← register_all() 统一注册
│       ├── exec_tool.py
│       ├── file_tool.py
│       ├── memory_tool.py
│       └── system_tool.py
│
├── skills/              ⚠️ 仅 loader.py 完成
│   └── loader.py        ✅ SKILL.md 解析加载
│
├── mcp/                 ✅ 已完成（Phase 6）
│   ├── __init__.py       ✅ 包入口，导出所有 public API
│   ├── client.py         ✅ McpClient + 双传输 + 状态机 + Info/Result 数据类
│   ├── tool_adapter.py   ✅ McpToolCallHandler / ResourceHandler / PromptHandler
│   └── provider.py       ✅ MCPToolProvider（ToolProvider ABC 实现）
├── scheduler/           ⚠️ 最简版本
│   └── reminder.py      ✅ 一次性提醒
│
├── debug/               ❌ Phase 5 已删除（合并到 agent/logger.py）
│
├── model_router_config.yaml  ← 路由配置（providers/models/purposes 三层）
│
└── tests/               ✅ Phase 1-6 测试
    ├── test_phase1_acceptance.py
    ├── test_phase2_acceptance.py
    ├── test_phase3_acceptance.py
    ├── test_phase4_acceptance.py
    ├── test_phase5_acceptance.py
    └── test_phase6_acceptance.py
```

> 图例：✅ 已完成 ⚠️ 部分完成 📋 规划中

## 当前实现状态

| 模块 | 状态 | 说明 |
|------|------|------|
| 配置系统 | ✅ 完整 | YAML → dataclass，RouterConfig + 后向兼容；expand_env_vars 未解析时 WARNING |
| LLM 抽象层 | ✅ 完整 | Message 含 tool_calls；ChatChunk 流式；LLMClient ABC |
| OpenAICompatibleClient | ✅ 完成 | 通用基类：chat() / _convert_messages() / _parse_stream_chunk() / 流式状态管理 |
| Qwen 客户端 | ✅ 完成 | 继承 OpenAICompatibleClient，33 行 |
| DeepSeek 客户端 | ✅ 完成 | 继承 OpenAICompatibleClient，33 行 |
| OpenAI 客户端 | ✅ 完成 | 继承 OpenAICompatibleClient，33 行 |
| ModelRouter | ✅ 完成 | priority 确定性选择 + 降级链自动生成 + forced_model 三层匹配 |
| LLM Proxy | ✅ 完成 | ModelRouter + RateLimiter + CallSetupError/NonRetryableStreamError 降级 |
| Agent 循环 | ✅ 完成 | ReAct + assistant(tool_calls) + AgentContext + PromptBuilder + AgentResult |
| AgentContext | ✅ 完成 | frozen=True 不可变快照，13 字段 |
| AgentResult | ✅ 完成 | 含 tool_calls_count/iterations/duration_ms/error；__str__/__eq__/__contains__ str 兼容 |
| PromptBuilder | ✅ 完成 | DataProvider 模式，3 个 Provider（Role/Rules/Tools），容错跳过 |
| AgentLogger | ✅ 完成 | request_id 全链路追踪，回写 DebugManager 保持 /debug 兼容 |
| message_utils | ✅ 完成 | validate/trim/clean 三个纯函数，trim 含 assistant-tool 配对保护 |
| CLI Channel | ✅ 完整 | rich 渲染，流式输出，print_* 方法 |
| common/ 工具库 | ✅ 完成 | rate_limiter（Token Bucket + Lock）、singleton（SingletonMeta）、utils |
| 会话管理 | ✅ 完整 | JSON 存储，多会话 CRUD；load() 返回 SessionMessage 对象 |
| MemoryStorage | ✅ 完成 | SQLite WAL + FTS5 双索引（unicode61/trigram）+ embedding BLOB + 文件变更检测 |
| TextChunker | ✅ 完成 | Markdown 结构感知分块（不切断 ## 标题，块间重叠） |
| EmbeddingProvider | ✅ 完成 | OpenAIEmbeddingProvider + LRU EmbeddingCache；numpy 缺失纯 Python 降级 |
| MemoryManager | ✅ 完成 | 混合检索（向量 0.7 + 关键词 0.3）+ sync 文件同步 + flush 触发 + 时间衰减 |
| MemoryFlushManager | ✅ 完成 | L2 日记忆 LLM 摘要 + 同日 content hash 去重 + 降级模板 |
| DeepDream | ✅ 完成 | L3 蒸馏 LLM 语义合并 + MEMORY.md.bak 备份 + .dream_state.json |
| MemoryProvider | ✅ 激活 | P3 骨架 → 从 context.memory_summary 读取语义检索结果注入 Prompt |
| 工具系统 | ✅ 完整 | Phase 5 架构重构：ToolHandler ABC / ToolRegistry 纯注册 / ToolExecutor 调度层三层分离；builtin/ 子包 8 个工厂函数；ApprovalManager 从 config 加载；ToolProvider ABC 骨架预留 MCP/Skill |
| MCP 集成 | ✅ 完成 | Phase 6：双传输（stdio+Streamable HTTP）+ McpClient 状态机 + 三个独立 Handler（Tool/Resource/Prompt）+ MCPToolProvider（ToolProvider ABC）+ /mcp 命令 + /tools 按来源分组 |
| DebugManager | ❌ Phase 5 已删除 | TraceRecord 合并到 AgentLogger，/debug 命令从 AgentLogger.get_last_trace() 读取 |
| 测试 | ✅ 完成 | P1: 7/7 + P2: 7/7 + P3: 8/8 + P4: 6/6 + P5: 39/39 + P6: 28/28 = 95/95 全部通过 |
| Skill 加载 | ⚠️ 半成品 | 能加载 SKILL.md，未注入 system prompt |
| 定时提醒 | ⚠️ 最简版 | 仅一次性延迟提醒 |
| MCP 集成 | ❌ 未实现 | 不支持 MCP 协议 |
| python 工具 | ❌ 未注册 | 列入后续更新建议，依赖条件加载机制 |

### 规划模块重要性说明

| 模块 | 优先级 | 规划 Phase | 为什么重要 |
|------|--------|-----------|------------|
| `common/` 工具库 | 🔴 高 | Phase 2 ✅ | 多模型必有限流（API rate limit），后续工具系统需要并发控制 |
| `agent/context.py` | 🔴 高 | Phase 3 ✅ | 将散落在 config/session/llm 的上下文统一管理；frozen=True 不可变快照 |
| `agent/result.py` | 🟡 中 | Phase 3 ✅ | run() 返回 AgentResult（含 tool_calls_count/iterations/duration_ms/error），__str__ 兼容 |
| `agent/prompt/builder.py` | 🔴 高 | Phase 3 ✅ | DataProvider 模式模块化 system prompt；P4/P7 只需新增 Provider 不修改 Builder |
| `agent/message_utils.py` | 🟡 中 | Phase 3 ✅ | validate/trim/clean 纯函数；trim 含 assistant(tool_calls)-tool 配对保护 |
| `memory/flush.py` | 🔴 高 | Phase 4 ✅ | L2 日记忆 LLM 摘要写入 + 同日去重；AgentLoop.run() 末尾异步触发 |

---

## 与 CowAgent 的对比分析

dotClaw 参考项目 **CowAgent**（位于 `D:\dev\CowAgent`）是一个成熟的、**生产级的 AI Agent 支撑框架**（Agent Harness），支持 16+ LLM 供应商、9 种 IM 通道、完整的三级记忆系统 + Deep Dream 蒸馏等。dotClaw 定位为**轻量级学习框架**，两者定位存在根本差异：

| 维度 | dotClaw（学习框架） | CowAgent（生产级框架） |
|------|---------------------|------------------------|
| **定位** | 学习 AI Agent 开发 | 超强 AI 助手（Super AI Assistant） |
| **代码规模** | ~40 个 Python 文件 | ~200+ 个 Python 文件 |
| **LLM 支持** | Qwen + DeepSeek + OpenAI（3 供应商） | 16+ 供应商，OpenAICompatibleBot 混入模式 |
| **通道** | CLI（可扩展） | 9 种：Web、微信、飞书、钉钉、企微、QQ 等 |
| **记忆系统** | 单文件 MEMORY.md | 三级架构：上下文 → 日记忆 → MEMORY.md + Deep Dream 蒸馏 |
| **上下文管理** | 未实现 | 分层压缩策略 + LLM 摘要注入 + 溢出恢复 |
| **工具系统** | 8 个基础工具 | 17 个工具 + MCP 协议集成 + 条件加载 |
| **工具健壮性** | 基础重试 | 连续失败检测、无限循环保护、JSON 修复 |
| **Skill 系统** | 加载 + 未注入 | Skill Hub 生态 + 对话式创建器 |
| **取消机制** | 无 | CancelTokenRegistry + 安全检查点 |
| **配置系统** | YAML → dataclass + RouterConfig | JSON + 环境变量覆盖 + 85 配置项 |
| **通道模式** | 同步输入/输出 | 生产者-消费者 + 线程池 |
| **部署** | pip install -e . | Docker + 多平台脚本 |

### dotClaw 可借鉴的设计（按优先级排序）

以下 CowAgent 的核心设计对 dotClaw 有直接参考价值，已融入下方更新后的开发路线图：

1. **三级记忆 + Deep Dream 蒸馏**：上下文 → 日记忆 → MEMORY.md，夜间 LLM 自动蒸馏 → Phase 4
2. **上下文分层压缩策略**：根据消息类型和轮次动态截断，工具结果分级截断 → Phase 4
3. **OpenAICompatibleBot 混入模式**：一个基类支持所有 OpenAI 兼容 LLM → Phase 2 ✅
4. **工具失败恢复机制**：相同参数连续失败/循环检测、JSON 参数修复 → 后续更新
5. **条件性工具加载**：根据环境变量/依赖自动决定工具是否可用 → 后续更新
6. **MCP 协议集成**：动态工具注册 + 增量热重载 → Phase 6
7. **Skill 创建工作流**：对话式引导创建 SKILL.md → Phase 7
8. **Cancel Token Registry**：请求级/会话级取消 + 安全检查点 → Phase 9

### dotClaw 不需要借鉴的部分

这些 CowAgent 功能超出 dotClaw 作为学习框架的定位：

- **16+ LLM 供应商**：dotClaw 支持 3-5 个关键供应商即可（Qwen + DeepSeek + OpenAI）
- **9 种 IM 平台**：dotClaw 只需 CLI + 可选 Web UI
- **TTS/语音/翻译**：非 Agent 框架核心功能
- **Docker/K8s 部署**：学习框架不需要容器化
- **插件系统**：过于复杂，Skill 系统可替代
- **Web 控制台**：远期可选

---

---

## 开发路线图

> 依赖链：P1 → P2 → P3 → P4 → P5 → P6 → P7 → P8/P9 → P10
> （P8 和 P9 无相互依赖，可并行）

---

### Phase 1：Agent 核心循环（已完成 ✅）

**总体内容描述**

实现完整 ReAct 循环，使框架能真正运行：接收用户输入 → 调用 LLM → 处理工具调用 → 循环直至返回最终文本 → 保存会话。本阶段是所有后续开发的基础。

**完成日期**：2026-05-28

**分模块修改清单**

| 文件 | 修改内容 |
|------|----------|
| `src/dotclaw/llm/base.py` | Message 新增 `tool_calls` 字段 |
| `src/dotclaw/llm/qwen.py` | 修复流式 args 拼接；`_convert_messages` 序列化 tool_calls |
| `src/dotclaw/agent/loop.py` | 完整 ReAct 循环；`_build_messages()` 方法；assistant(tool_calls) 消息；流式输出；工具调用通知 |
| `src/dotclaw/main.py` | 初始化 ToolRegistry/ApprovalManager 并传入 AgentLoop；`/tools` 命令；日志屏蔽 |
| `src/dotclaw/tools/base.py` | `execute()` 从 config 读取 needs_approval |
| `src/dotclaw/tools/__init__.py` | 新建，自动注册所有工具模块 |
| `src/dotclaw/channel/base.py` | 添加 print_error/print_info/print_markdown 默认实现 |
| `src/dotclaw/channel/cli.py` | 去掉 Console.print 不支持的 flush 参数 |
| `src/dotclaw/memory/store.py` | `_dict_to_session()` 确保 load 返回 SessionMessage 对象 |
| `tests/test_phase1_acceptance.py` | 新建，5 个场景 + 2 个回归验证 |

**验收标准**（全部通过 ✅）

1. 纯文本对话：流式输出，会话持久化
2. 带工具调用的对话：LLM 返回 tool_call → 执行工具 → 结果返回 LLM → 最终文本
3. 危险工具审批：exec 工具执行前请求用户确认
4. 调试追踪：`/debug` 命令显示最近推理过程
5. 多轮对话：会话历史加载正确，Agent 能记住上下文

**开发注意事项**

- AgentLoop 的 `_build_messages()` 设计为独立方法，为后续 Prompt Builder 替换预留接口
- ToolRegistry 通过依赖注入传入，不依赖全局单例
- 流式输出的 channel.stream() 需配合 channel.send("\n") 完成换行

---

### Phase 2：多模型支持 + 基础设施（已完成 ✅）

> 详细开发文档：[docs/phase2-roadmap.md](phase2/phase2-roadmap.md)

**总体内容描述**

将 LLM 层从单一 Qwen 客户端升级为多供应商架构：1 个通用基类（OpenAICompatibleClient）+ 3 个具体客户端（Qwen/DeepSeek/OpenAI）+ priority 确定性路由 + 跨供应商降级。新增 `common/` 通用工具库（限流器、单例、工具函数）。配置系统扩展为 `model_router_config.yaml`（providers / models / purposes 三层），后向兼容旧 `config.yaml`。

**完成日期**：2026-05-29

**分模块修改清单**

| 模块 | 文件 | 状态 | 描述 |
|------|------|------|------|
| 通用基类 | `llm/openai_compat.py` | 新增 | chat() / _convert_messages() / _parse_stream_chunk() / 流式状态。三个子类钩子 |
| QwenClient | `llm/qwen.py` | 重构 | 从 142 行 → 33 行，继承 OpenAICompatibleClient |
| DeepSeekClient | `llm/deepseek.py` | 新增 | 继承 OpenAICompatibleClient |
| OpenAIClient | `llm/openai.py` | 新增 | 继承 OpenAICompatibleClient |
| ModelRouter | `llm/model_router.py` | 新增 | purpose → priority 确定选择（priority 越小越优先）；降级链从 priority 自动生成；forced_model 三层匹配 |
| LLMProxy | `llm/proxy.py` | 重构 | ModelRouter + RateLimiter；CallSetupError → 降级 / NonRetryableStreamError → 不降级；降级直接查 models 字典不回环 |
| 限流器 | `common/rate_limiter.py` | 新增 | 令牌桶算法，asyncio.Lock 并发安全，按 provider 维度限流 |
| 单例工具 | `common/singleton.py` | 新增 | SingletonMeta 元类，支持 reset() |
| 工具函数 | `common/utils.py` | 新增 | expand_env_vars()（未解析 WARNING）/ safe_load_yaml() |
| 路由配置 | `model_router_config.yaml` | 新增 | providers / models / purposes 三层 |
| 配置扩展 | `config/settings.py` | 修改 | 新增 RouterConfig 等 7 个 dataclass；load_router_config() / _build_router_config_from_legacy() |
| AgentLoop | `agent/loop.py` | 微调 | llm.chat() 增加 purpose="chat" |
| main.py | `main.py` | 修改 | P2 路由初始化：RouterConfig → ModelRouter → RateLimiter → LLMProxy |
| 测试 | `tests/test_phase2_acceptance.py` | 新增 | 7 个场景：等价性/优先级/FM匹配/降级/限流/流式不降级/调用前降级 |

**分模块内容要点**

- **LLM 继承链**：`LLMClient(ABC)` → `OpenAICompatibleClient` → QwenClient / DeepSeekClient / OpenAIClient。通用逻辑全在基类，子类仅覆写 `_get_api_key()` / `_get_base_url()` / `_get_model_id()`
- **路由规则**：默认走 `purposes.chat.priority` 升序取第一个 active；forced_model 走三层匹配（精确 models key → 前缀 providers key → defaults）；降级按 priority 顺序依次尝试，跳过不存在的模型，不回落到 default
- **降级设计**：`CallSetupError`（调用前异常，如连接超时/认证失败）触发降级；`NonRetryableStreamError`（流式中途异常）不降级直接抛出。降级时直接查 models 字典，不经过 `router.resolve()` 避免回环
- **后向兼容**：`model_router_config.yaml` 不存在时从旧 `config.yaml` 的 `llm.clients` 自动构建 RouterConfig，行为等价 P1

**验收标准**（全部通过 ✅）

1. 单供应商纯文本对话：QwenClient 被调用，流式输出正常
2. 模型切换：`/model deepseek-v3` 后使用 DeepSeekClient
3. 跨供应商降级：无效 base_url → 降级到下一个模型 → 成功返回
4. 多轮工具调用：tool_call → 执行 → 返回 LLM → 最终文本
5. 限流保护：超配额时 await 等待
6. 后向兼容：无 router config 时降级到 P1 行为

**开发注意事项**

- `expand_env_vars` 未解析的环境变量打印 WARNING 日志，不会静默通过
- `_instantiate_client` 未知 provider 名称回退到 QwenClient（通用兼容）
- 限流器默认关闭（`requests_per_minute: 0`）

---

### Phase 3：Agent 内部基础设施（已完成 ✅）

> 详细开发文档：[docs/phase3-roadmap.md](phase3/phase3-roadmap.md)

**总体内容描述**

建立 Agent 核心基础设施层：AgentContext 不可变上下文快照、DataProvider 模式的 PromptBuilder、AgentResult 标准化返回类型、message_utils 纯函数工具集、AgentLogger 结构化日志（request_id 追踪）。为后续记忆注入（P4）、工具动态注册（P5）、Skill 注入（P7）提供统一的数据通道和接口规范。

**完成日期**：2026-05-30

**分模块修改清单**

| 模块 | 文件 | 状态 | 描述 |
|------|------|------|------|
| AgentResult | `agent/result.py` | 新增 | 纯 dataclass（final_text/tool_calls_count/iterations/duration_ms/error/request_id）；`__str__`/`__eq__`/`__contains__` 保持 str 兼容 |
| message_utils | `agent/message_utils.py` | 新增 | validate/trim/clean 三个纯函数；trim 含 assistant(tool_calls)-tool 配对保护算法；中英文差异化 token 估算 |
| AgentLogger | `agent/logger.py` | 新增 | 封装 logging，request_id 全链路追踪；`new_request()` 每次 run() 生成独立 ID；`record()` 回写 DebugManager |
| AgentContext | `agent/context.py` | 新增 | frozen=True 不可变 dataclass，13 个字段；`_build_context()` 在 run() 开头组装；workspace 默认=project_root |
| DataProvider | `agent/prompt/providers.py` | 新增 | DataProvider ABC + Role/Rules/Tools 三个 Provider；Memory/Skills 骨架（P4/P7 激活） |
| PromptBuilder | `agent/prompt/builder.py` | 新增 | 遍历 providers 拼接 system prompt；Provider 异常容错（warning 跳过不中断） |
| AgentLoop | `agent/loop.py` | 修改 | `_build_context()` 组装 AgentContext；`_build_messages()` 调用 PromptBuilder + message_utils.trim/clean；`run()` 返回 AgentResult |
| AgentConfig | `config/settings.py` | 修改 | 新增 `rules: str` 字段 + YAML 解析 |
| main.py | `main.py` | 修改 | 初始化 AgentLogger + PromptBuilder（Role/Rules/Tools），传入 AgentLoop |
| 测试 | `tests/test_phase3_acceptance.py` | 新增 | 8 个场景：兼容性/不可变/拼接/容错/验证/裁剪/估算/request_id |

**架构变化**

```
P2:  run() → 直接拼 system_prompt → 返回 str

P3:  run() → _build_context() → AgentContext(frozen)
         ↓
     _build_messages():
       PromptBuilder.build(context)  →  Role + Rules + Tools
       msg_trim(messages, max_tokens)  →  配对保护裁剪
       msg_clean(messages)             →  去空/去重/去孤立
         ↓
     返回 AgentResult(final_text, tool_calls_count, iterations, ...)
         ↓
     AgentLogger.record() → 回写 DebugManager（/debug 可用）
```

**分模块内容要点**

- **AgentContext**：`frozen=True`，每次 `run()` 开头由 `_build_context()` 创建不可变快照。包含 session_id、model、system_prompt、tool_definitions、rules、request_id、channel 等 13 个字段。workspace 默认等于 project_root，P7/P10 可独立设置
- **PromptBuilder**：`build(context)` 按顺序调用 `DataProvider.provide()`，拼接 role → rules → tools section。P4 新增 `MemoryProvider`、P7 新增 `SkillsProvider`，不改 Builder
- **AgentResult**：替代裸 `str`，但 `__str__`/`__eq__`/`__contains__` 全部兼容旧代码。`result == "text"` 和 `"text" in result` 直接可用
- **message_utils.trim()**：从旧到新逐条裁剪，保护 system 消息不裁、保护 assistant(tool_calls) + tool 配对组不被拆散。P3 用中英文差异化估算 token，P4 升级 tiktoken
- **AgentLogger**：每次 `run()` 调用 `new_request()` 生成唯一 8 位 request_id。日志回写 DebugManager 保持 `/debug` 命令可用（双日志系统为临时状态，P5 后合并）

**验收标准**（全部通过 ✅）

1. AgentResult 兼容：`str(result) == result.final_text`，`result == "text"`，`"text" in result`
2. AgentContext 不可变：frozen=True，修改字段抛出异常
3. PromptBuilder 拼接：3 个 section 正确输出，分隔符正确
4. PromptBuilder 容错：Provider 异常不影响其他 section
5. message_utils 合法性验证：合法对话 0 issue，孤立 tool 被检测
6. message_utils 裁剪保护：配对组不被拆散
7. request_id 追踪：两次 run() 生成不同 request_id
8. `/debug` 兼容：P3 后 `/debug` 命令仍正常工作

**开发注意事项**

- `config.agent.rules` 为空字符串时 `RulesProvider.provide()` 返回 None（跳过 section）
- frozen dataclass 的 workspace 默认值需用 `str(workspace) == "."` 检测（`not Path(".")` 在 Windows 上为 True）
- AgentLogger 与 DebugManager 双日志系统并存是临时技术债，P5 后合并

---

### Phase 4：记忆系统（已完成 ✅）

> 详细开发文档：[docs/phase4-roadmap.md](phase4/phase4-roadmap.md) | 变更日志：[docs/phase4-record.md](phase4/phase4-record.md)

**总体内容描述**

实现三级记忆架构：L1 Session JSON（P1 已有）→ L2 日记忆文件（LLM 摘要 + SQLite 索引）→ L3 MEMORY.md（Deep Dream LLM 语义蒸馏）。使用 SQLite FTS5 双索引（unicode61 + trigram）+ numpy 向量余弦相似度混合检索。P3 预留的 MemoryProvider 骨架激活，token 估算保留中英文差异化公式。

**完成日期**：2026-05-31 ~ 2026-06-01

**分模块修改清单**

| 模块 | 文件 | 状态 | 描述 |
|------|------|------|------|
| MemoryStorage | `memory/storage.py` | 新增 | SQLite WAL + FTS5 双索引 + embedding BLOB + 文件变更检测 + numpy 降级 |
| TextChunker | `memory/chunker.py` | 新增 | Markdown 结构感知分块（不切断 ## 标题，块间重叠） |
| EmbeddingProvider | `memory/embedding.py` | 新增 | OpenAIEmbeddingProvider + LRU EmbeddingCache（OrderedDict, max 256） |
| MemoryManager | `memory/manager.py` | 新增 | 混合检索 + sync 文件同步 + flush 触发 + 时间衰减 + 递归防护 |
| MemoryFlushManager | `memory/flush.py` | 新增 | L2 日记忆 LLM 摘要 + 同日 content hash 去重 + 降级模板 |
| DeepDream | `memory/dream.py` | 新增 | L3 蒸馏 LLM 语义合并 + MEMORY.md.bak 备份 + .dream_state.json |
| AgentContext | `agent/context.py` | 修改 | 新增 `memory_summary` 字段 |
| MemoryProvider | `agent/prompt/providers.py` | 修改 | P3 骨架 → 读 context.memory_summary → "## 相关记忆" section |
| AgentLoop | `agent/loop.py` | 修改 | memory_mgr 参数；`_build_context` async 语义检索；flush 异步触发 |
| MemoryConfig | `config/settings.py` | 修改 | 扩展 20 字段 + `_resolve_memory_path` + P4 字段 YAML 解析 |
| main.py | `main.py` | 修改 | 记忆初始化链 + `/dream` 命令 |
| pyproject.toml | `pyproject.toml` | 修改 | 新增 `numpy>=1.26.0` |
| 测试 | `tests/test_phase4_acceptance.py` | 新增 | 6 个场景：CRUD/向量/分块/降级/缓存/注入 |

**架构变化**

```
P3:  run() → _build_context()[同步] → AgentContext → PromptBuilder

P4:  run() → _build_context()[async]
         │           │
         │     MemoryManager.search(user_message)
         │           │
         │     混合检索 + 时间衰减
         │           │
         ▼           ▼
    AgentContext(memory_summary="...")
         │
    PromptBuilder → MemoryProvider → "## 相关记忆"
         │
    run() 末尾: asyncio.create_task(flush_memory())
         │
    L2: LLM 摘要 → YYYY-MM-DD.md
    L3: /dream → LLM 蒸馏 → MEMORY.md
```

**分模块内容要点**

- **三级记忆**：L1 `Session.messages`（JSON，P1）→ L2 `data/memory/YYYY-MM-DD.md`（LLM 2-3 句中文摘要）→ L3 `data/memory/MEMORY.md`（Deep Dream 语义蒸馏，写入前备份 `.bak`）
- **混合检索**：向量余弦相似度（权重 0.7）+ FTS5 trigram 关键词（权重 0.3）加权合并。日记忆按半衰期 30 天指数衰减，MEMORY.md 不衰减
- **flush**：消息数 > `flush_threshold`（20）→ `asyncio.create_task` 异步 LLM 摘要 → 同日 content hash 去重。仅正常路径触发
- **Deep Dream**：`/dream` 手动触发 → `.dream_state.json` 记录状态，同日期不重复蒸馏
- **numpy 降级**：未安装时 `search_vector()` 降级为纯 Python 余弦相似度

**验收标准**（全部通过 ✅）

1. CRUD + 中英文关键词搜索 + UPSERT rowid 稳定性
2. 向量检索排序 + embedding BLOB round-trip
3. TextChunker 不切断 ## 标题边界
4. embedding=None → 纯关键词降级
5. EmbeddingCache LRU 淘汰
6. MemoryProvider 空/非空 memory_summary 注入

**开发注意事项**

- FTS5 UPSERT 后需 `rebuild` content table（小数据集可接受，设计权衡）
- FTS5 trigram 短中文（<3 字符）走 LIKE 降级
- token 估算保留 P3 中英文差异化公式（已移除 tiktoken）
- AgentLogger + DebugManager 双日志系统 P5 合并

---

### Phase 5：工具层架构重构（已完成 ✅）

> 详细开发文档：[docs/phase5-roadmap.md](phase5/phase5-roadmap.md) | 变更日志：[docs/phase5-record.md](phase5/phase5-record.md)

**总体内容描述**

工具层架构重构：ToolHandler ABC 统一工具抽象（builtin/MCP/Skill）、ToolRegistry 纯注册表（注册/查询分离）、ToolExecutor 调度层（审批+超时+错误处理）、ApprovalManager 去硬编码、builtin/ 子包迁移 8 个内置工具、ToolProvider ABC 骨架预留 MCP/Skill 接口。合并 P3 遗留技术债——TraceRecord + DebugManager 整合到 AgentLogger，删除 debug/ 子包。配置系统扩展 ToolsConfig（source 级启停 + approval_commands + disabled_tools + exec_timeout），旧格式 per-tool 自动向后兼容转换。

**完成日期**：2026-06-03

**分模块修改清单**

| 模块 | 文件 | 状态 | 描述 |
|------|------|------|------|
| 工具基础 | `tools/base.py` | 重构 | ToolDefinition 扩展（source/needs_approval/timeout/metadata）+ ToolResult 结构化（error_code/error_type/metadata）+ ToolExecutionContext（仅 timeout）。删除全局 _registry 和 register_tool 装饰器 |
| 工具抽象 | `tools/handler.py` | 新增 | ToolHandler ABC 统一接口（definition + execute）+ BuiltinToolHandler 适配器包装现有异步函数 |
| 注册表 | `tools/registry.py` | 新增 | ToolRegistry 纯注册表（register/get/list/unregister/clear/list_by_source），同名静默覆盖，不含 execute() |
| 调度层 | `tools/executor.py` | 新增 | ToolExecutor：审批检查（needs_approval + approval_commands AND 双重门）+ asyncio.wait_for 超时控制 + 结构化错误处理（TOOL_NOT_FOUND / TIMEOUT / APPROVAL_DENIED / EXECUTION_ERROR） |
| 审批管理 | `tools/approval.py` | 重构 | 删除硬编码 NEEDS_APPROVAL，改用 _approval_commands 集合从 config 加载；set_approval_commands() 热更新 |
| 内置工具 | `tools/builtin/` | 新增子包 | exec/file/memory/system 四类 8 个工具迁移为 BuiltinToolHandler 工厂函数 + `register_all()` 统一注册入口 |
| Provider 接口 | `tools/provider.py` | 新增骨架 | ToolProvider ABC 定义 discover_and_register() 接口，为 MCP/Skill 预留标准注入入口（Phase 5 不实现） |
| 工具导出 | `tools/__init__.py` | 修改 | 导出 ToolRegistry/ToolExecutor/ToolProvider/ToolHandler 等新架构模块 |
| 日志合并 | `agent/logger.py` | 修改 | TraceRecord 从 debug/logger.py 迁移到此处；AgentLogger 新增 level/log_file 参数 + _setup_logging() + 直接管理 _last_trace（不再委托 DebugManager） |
| Agent 循环 | `agent/loop.py` | 修改 | 参数 `tool_registry` → `tool_executor`；删除 `_debug_manager`；`debug_trace()` 从 `_logger.get_last_trace()` 读取；AgentLogger 从 TYPE_CHECKING 移到顶层 import |
| 主入口 | `main.py` | 修改 | 组装新架构：ToolRegistry → builtin.register_all() → ApprovalManager → ToolExecutor → AgentLoop；删除 DebugManager 引用；/tools 命令从 ToolExecutor.get_handler() 读取审批状态 |
| 配置扩展 | `config/settings.py` | 修改 | ToolsConfig 新增 builtin_enabled/mcp_enabled/skill_enabled + approval_commands + disabled_tools + exec_timeout。_raw_to_config() 向后兼容旧 per-tool 格式 |
| 配置文件 | `config.yaml` | 修改 | tools 段结构更新：source 级启停 + approval_commands 列表 + disabled_tools |
| 旧文件删除 | `tools/{exec,file,memory,system}_tool.py` | 删除 | 已迁移到 builtin/ 子包 |
| 旧子包删除 | `debug/` | 删除整体 | TraceRecord + DebugManager 已合并到 agent/logger.py；`import dotclaw.debug` 报 ModuleNotFoundError |
| 代码审查修复 | `tools/builtin/exec_tool.py` + `file_tool.py` | 修改 | W1：CancelledError 处理防孤儿子进程；W2：MAX_FILE_SIZE 10MB 大小限制 |
| 验收测试 | `tests/test_phase5_acceptance.py` | 新增 | 39 tests / 15 场景：注册/注销/执行/审批/超时/未找到/集成/配置/日志/兼容/定义/工厂/Provider/CancelledError/文件大小 |

**架构变化**

```
P4:  tools/base.py                    — ToolDefinition + ToolResult + global _registry + register_tool + ToolRegistry(含execute)
     tools/{exec,file,memory,system}_tool.py — 各自 @register_tool 装饰器
     tools/approval.py                — NEEDS_APPROVAL = {"exec", "python"} 硬编码
     debug/logger.py                  — TraceRecord + DebugManager
     agent/logger.py                  — AgentLogger（从 debug/logger.py import TraceRecord）
     agent/loop.py                    — _tool_registry.execute() + _debug_manager
     main.py                          — ToolRegistry(approval_mgr, config)

P5:  tools/base.py                    — ToolDefinition（+source/needs_approval/timeout/metadata） + ToolResult（+error_code/error_type/metadata） + ToolExecutionContext
     tools/handler.py                 — ToolHandler ABC + BuiltinToolHandler（Adapter）
     tools/registry.py                — ToolRegistry（纯注册，无 execute）
     tools/executor.py                — ToolExecutor（调度+审批+超时+错误）
     tools/approval.py                — ApprovalManager（_approval_commands 从 config 加载）
     tools/builtin/{exec,file,memory,system}_tool.py — 工厂函数返回 BuiltinToolHandler
     tools/provider.py                — ToolProvider ABC（骨架）
     agent/logger.py                  — AgentLogger + TraceRecord（合并，直接管理 _last_trace + _setup_logging）
     agent/loop.py                    — _tool_executor.execute() + _logger（无 _debug_manager）
     main.py                          — ToolRegistry() → register_all() → ApprovalManager() → ToolExecutor(registry, approval)
     ❌ tools/{exec,file,memory,system}_tool.py（旧） — 删除
     ❌ debug/ 子包                                    — 删除整体
```

**数据通路（启动 + 运行时）**

```
启动阶段：
  main.py
    → ToolRegistry()
    → config.tools.builtin_enabled? → register_all(tool_registry)  # 注册 8 个 BuiltinToolHandler
    → config.tools.disabled_tools → tool_registry.unregister()
    → ApprovalManager(approval_commands=config.tools.approval_commands)
    → ToolExecutor(registry, approval_manager)
    → AgentLogger(level=config.debug.level, log_file=config.debug.log_file)
    → AgentLoop(tool_executor=..., logger=...)

运行时：
  AgentLoop.run()
    → LLM returns tool_calls
      → for each tc:
          tool_executor.execute(name, args, channel)
            → registry.get(name) → handler
            → handler.definition().needs_approval?
              → approval.check(name, args, channel)
                → tool_name in _approval_commands?
                  → channel.ask_user() → y/n
            → asyncio.wait_for(handler.execute(args, ctx), timeout)
            → return ToolResult
```

**验收标准**（全部通过 ✅）

1. 内置工具全部可用：exec/read_file/write_file/list_dir/memory_read/memory_write/system_info/get_time 共 8 个，工具名无命名空间前缀，行为与 Phase 4 前完全一致
2. 审批机制生效：config.yaml approval_commands 中配置的工具，needs_approval=True 时触发用户确认；拒绝返回 APPROVAL_DENIED；不在列表中则放行
3. 超时控制生效：工具超时返回 TIMEOUT error_code
4. 结构化错误：不存在工具返回 TOOL_NOT_FOUND；异常捕获返回 EXECUTION_ERROR
5. 日志合并：/debug 命令从 AgentLogger.get_last_trace() 获取；debug/ 子包已删除，import dotclaw.debug 报 ModuleNotFoundError
6. AgentLoop 行为不变：LLM 返回内容、工具调用次数、iterations 与 Phase 4 前一致
7. 架构验收：ToolRegistry 只含注册/查询，不含 execute；ToolExecutor 负责调度；ToolHandler ABC 定义清晰
8. 配置向后兼容：旧格式 per-tool needs_approval/enabled/timeout 自动转换为新格式 approval_commands/disabled_tools/exec_timeout
9. 代码质量：ToolRegistry 纯注册无副作用；asyncio.wait_for 超时控制；ApprovalManager 无硬编码
10. 回归：Phase 1-4 全部 28 测试通过

**开发注意事项**

- 工具无状态：ToolExecutionContext 只含 timeout，不含 session/workspace 等状态
- 后注册覆盖：ToolRegistry.register() 同名直接覆盖，无警告——Phase 6/7 决定是否需要日志
- source 级启停：config.yaml 按 builtin_enabled/mcp_enabled/skill_enabled 控制
- 审批双重机制：ToolDefinition.needs_approval（工具声明）+ approval_commands（用户配置）AND 关系
- 超时直接杀：asyncio.wait_for 超时后 cancel task，子进程由 Handler 内部 kill（exec_tool 已有 proc.kill() + CancelledError 防护）
- ToolResult 结构化：新增 error_code/error_type，不强制消费——现有逻辑只使用 output，向后兼容
- 内置工具函数不变：通过 BuiltinToolHandler Adapter 调用，不修改原有函数签名
- needs_approval 默认 False：开发者新增危险工具时必须显式设置 needs_approval=True
- 最小改动 AgentLoop：仅 _tool_registry → _tool_executor，无其他侵入

**跨阶段依赖声明**

以下内容在 Phase 5 开发中产生，交付给后续 Phase：

| 依赖方向 | 内容 | 说明 |
|----------|------|------|
| → Phase 6 | ToolProvider ABC | `tools/provider.py` 已定义 `discover_and_register(registry)` 接口。MCPToolProvider 只需实现此接口，调用 `registry.register(McpToolHandler(...))` 即可完成工具注册 |
| → Phase 6 | McpToolHandler | 需新建继承 `ToolHandler` ABC 的实现类，在 `execute()` 中通过 MCP 协议调用外部工具 |
| → Phase 6 | source 级启停 | `config.tools.mcp_enabled` 已预留字段，Phase 6 启动时检查此标志决定是否初始化 MCP |
| → Phase 7 | SkillToolProvider | 需实现 `ToolProvider` ABC，将 Skill 脚本注册为工具 |
| → Phase 7 | SkillsProvider 激活 | P3 预留的 `SkillsProvider` 骨架可激活——通过 `SkillLoader.build_skill_prompt()` 生成 system prompt 注入 |
| → Phase 7 | ToolDefinition.source = SKILL | 已定义 ToolSource.SKILL 枚举值，无需额外修改 |
| → Phase 8/9 | ApprovalManager 热更新 | `set_approval_commands()` / `set_enabled()` 已支持运行时修改审批列表，无需重启 Agent |
| → Phase 6 | disabled_tools 机制 | 已实现 `config.tools.disabled_tools` → `registry.unregister()` 管道，MCP 工具也可通过此机制禁用 |

**后续更新建议（不在 Phase 5 范围）**

| 优先级 | 模块 | 说明 |
|--------|------|------|
| 🔴 高 | 条件性工具加载 | ToolDefinition 新增 requires 字段（env vars / binaries / Python packages）。注册时检查，不满足则跳过 |
| 🔴 高 | 工具失败恢复机制 | AgentLoop / ToolExecutor 维护 tool_failure_history，检测连续失败/循环调用 |
| 🟡 中 | python 工具 | 新增 tools/python_tool.py，子进程隔离执行 Python 代码，需条件加载 |
| 🟡 中 | web_search / web_fetch 工具 | 新增 tools/web_search.py / tools/web_fetch.py，依赖条件加载机制 |

---

### Phase 6：MCP 协议集成（已完成 ✅）

> 详细开发文档：[docs/phase6/phase6-roadmap.md](./phase6/phase6-roadmap.md) | 变更日志：[docs/phase6/phase6-record.md](./phase6/phase6-record.md)

**总体内容描述**

实现 MCP（Model Context Protocol）客户端，支持 dotClaw 通过 MCP 协议动态调用外部工具。复用 Phase 5 预留的 ToolProvider ABC 和 ToolHandler ABC 接口——MCPToolProvider 实现 `discover_and_register()`，三个独立 Handler 类（McpToolCallHandler / McpResourceHandler / McpPromptHandler）分别实现 `ToolHandler` ABC。配置层继承 Phase 4/5 风格（dataclass + 解析函数 + 校验），支持 stdio（子进程）和 Streamable HTTP（远程服务）双传输。高可用设计：启动失败跳过、运行时崩溃自动重连（失败计数器 + 上限）、优雅关闭（session shutdown + transport terminate）。可观测性：`/tools` 按 ToolSource.BUILTIN/MCP 分组展示（MCP 按 server 二次分组），`/mcp` 命令显示连接状态（含 emoji 图标 + pending 状态）。

**完成日期**：2026-06-03

**分模块修改清单**

| 模块 | 文件 | 状态 | 描述 |
|------|------|------|------|
| 依赖 | `pyproject.toml` | 修改 | 新增 `mcp>=1.0.0` |
| 配置 dataclass | `config/settings.py` | 修改 | 新增 McpGlobalConfig（startup_timeout/tool_timeout/restart_on_crash/max_restart_attempts）+ McpServerConfig（name/transport/command/args/url/headers + 覆盖字段 + getter 方法 + __post_init__ 校验） |
| 配置解析 | `config/settings.py` | 修改 | 新增 _parse_mcp_global() + _parse_mcp_servers()（6 项校验：name 必填/重名检测/transport 合法/stdio 缺 command/streamable_http 缺 url）；ToolsConfig 扩展 mcp_global + mcp_servers 字段 |
| 配置文件 | `config.yaml` | 修改 | 新增 mcp_global 全局配置段 + mcp_servers 列表（含 stdio/streamable_http 示例注释） |
| MCP 包入口 | `mcp/__init__.py` | 新增 | 导出 McpClient / McpClientState / Handler 三件套 / Info 数据类 / MCPToolProvider |
| 客户端 | `mcp/client.py` | 新增 | McpClient 封装 mcp SDK（stdio + Streamable HTTP 双传输）+ 连接管理（connect/_cleanup_old_connection）+ 工具发现（_discover）+ 执行（call_tool/read_resource/get_prompt 均含 timeout 参数）+ 重连逻辑（_handle_execution_error）+ cancel 通知（_send_cancel）+ 优雅关闭（shutdown） |
| 状态机 | `mcp/client.py` | 新增 | McpClientState：STARTING/CONNECTED/CRASHED/FAILED/SHUTDOWN |
| 数据类 | `mcp/client.py` | 新增 | McpToolInfo/McpResourceInfo/McpPromptInfo（from_mcp 含 TYPE_CHECKING 类型标注）+ McpToolResult（from_mcp/from_resource_result/from_prompt_result，非文本 content 降级处理） |
| 异常 | `mcp/client.py` | 新增 | McpError → McpClientError / McpUnavailableError |
| 适配层 | `mcp/tool_adapter.py` | 新增 | McpToolCallHandler（tools/call）+ McpResourceHandler（resources/read，命名 read_{server}_{name}）+ McpPromptHandler（prompts/get，命名 prompt_{server}_{name}），三个类均实现 ToolHandler ABC + context.timeout fallback |
| Provider | `mcp/provider.py` | 新增 | MCPToolProvider（ToolProvider ABC 实现）— 编排：asyncio.gather 并行 connect → discover_and_register → register_handler；三层状态管理（_clients/_pending_servers/_failed_servers）；get_server_states()；shutdown() |
| 集成 | `main.py` | 修改 | MCP 初始化链：if mcp_enabled + mcp_servers → MCPToolProvider → asyncio.create_task 后台加载（保存 mcp_task 引用）；/quit 退出时 cancel task + provider.shutdown()；/tools 命令增强（按 ToolSource 分组 + MCP 按 server 二次分组）；/mcp 命令（状态展示含 ⏳✅💥❌🛑 图标）；帮助文本新增 /mcp |
| 验收测试 | `tests/test_phase6_acceptance.py` | 新增 | 28 tests / 7 场景：配置解析（8）/ 状态机（3）/ Info-Result（5）/ Handler 定义（3）/ Provider（5）/ 错误类型（2）/ 回归（2） |

**架构变化**

```
P5:  tools/                         — ToolRegistry / ToolExecutor / ToolHandler（builtin 工具注册）
     tools/provider.py              — ToolProvider ABC（骨架，无实现）
     tools/builtin/                 — 8 个内置工具工厂函数
     config/settings.py             — ToolsConfig（builtin_enabled/mcp_enabled/skill_enabled）

P6:  mcp/client.py                  — McpClient（双传输封装 + 状态机 + 重连 + cancel）
     mcp/tool_adapter.py            — 三个 Handler 类（Tool/Resource/Prompt 各自实现 ToolHandler ABC）
     mcp/provider.py                — MCPToolProvider（首个 ToolProvider ABC 实现）
     mcp/__init__.py                — 包入口
     config/settings.py             — ToolsConfig + mcp_global + mcp_servers + McpGlobalConfig/McpServerConfig
     config.yaml                    — mcp_global + mcp_servers 段
     main.py                        — MCP 后台加载 + /mcp 命令 + /tools 分组增强
```

**数据通路（启动 + 运行时）**

```
启动阶段（main.py）：
  if config.tools.mcp_enabled and config.tools.mcp_servers:
      mcp_provider = MCPToolProvider(global_config, server_configs, registry, approval_commands)
      mcp_task = asyncio.create_task(mcp_provider.start())
          → asyncio.gather(
              McpClient.connect() → initialize 握手 → _discover(tools/resources/prompts)
              → McpToolCallHandler / McpResourceHandler / McpPromptHandler
              → ToolRegistry.register(handler)
            )

运行时：
  AgentLoop.run()
    → LLM 返回 tool_calls（含 MCP 工具定义）
    → ToolExecutor.execute(name, args, channel)
      → registry.get(name) → McpToolCallHandler / McpResourceHandler / McpPromptHandler
      → handler.execute(args, ctx)
        → McpClient.call_tool(name, args, timeout) / read_resource(uri, timeout) / get_prompt(name, args, timeout)
        → McpToolResult → ToolResult

退出时：
  /quit → mcp_task.cancel() → await mcp_task
        → mcp_provider.shutdown()
          → 各 McpClient.shutdown() → session.shutdown() + transport.terminate()
```

**验收标准**（全部通过 ✅）

1. 配置解析：MCP 全局默认值正确（startup_timeout=4.0 / tool_timeout=60.0 / restart_on_crash=True / max_restart_attempts=3）
2. ServerConfig getter 正确：server 级超时覆盖全局默认值
3. 配置校验：重名/缺 command/缺 url/错 transport 等 6 种错误场景正确抛 ValueError
4. McpClientState 枚举值正确（starting/connected/crashed/failed/shutdown）
5. Info/Result 数据类：McpToolInfo/ResourceInfo/PromptInfo from_mcp() 正确解析 mcp SDK 对象；McpToolResult 文本/资源/prompt 三种结果正确
6. Handler 定义：source="mcp"，metadata 含 server/mcp_type；ResourceHandler 自动命名 read_{server}_{name}；PromptHandler 参数 schema 正确生成（required 字段 + properties）
7. Provider 生命周期：init/空启动/重复启动/shutdown 正确
8. Provider 实现 ToolProvider ABC：issubclass(MCPToolProvider, ToolProvider) 为 True
9. 异常层次：McpClientError / McpUnavailableError 正确继承 McpError
10. 回归：Phase 1-5 全部 67 测试通过

**开发注意事项**

- mcp SDK 依赖：`mcp>=1.0.0`，导入在 `connect()` 内部延迟执行（不影响模块顶层导入）
- stdio transport：子进程终止使用 `hasattr(self._transport, 'terminate')` 判断（defensive）
- streamable_http transport：导入路径 `mcp.client.http`（SDK 版本依赖，已知 SDK 版本差异风险）
- 重连保护：`_handle_execution_error()` 在 `MaxConnError` 时不重连（stdin 被占用）；计数器达到上限后标记 CRASHED
- 连接资源清理：`connect()` 开头 `_cleanup_old_connection()`（session.shutdown → transport.terminate → 置 None）
- Provider state leakage 修复：client 注册移到 handler 注册成功后
- /tools 分组：按 ToolSource.BUILTIN 和 ToolSource.MCP 分组，MCP 按 server 二次分组
- /mcp 命令：显示启动中/已连接/崩溃/失败/关闭五种状态，含 pending_servers
- MCP 工具与内置工具同一命名空间：后注册覆盖前注册，名称冲突时内置工具优先（先注册）

**跨阶段依赖声明**

以下内容在 Phase 6 开发中产生，交付给后续 Phase：

| 依赖方向 | 内容 | 说明 |
|----------|------|------|
| → Phase 7 | MCPToolProvider 参考 | SkillToolProvider 可参考 MCPToolProvider 的编排模式（asyncio.gather 并行 + 状态管理 + shutdown） |
| → Phase 7 | SkillsProvider 激活 | P3 预留的 SkillsProvider 可使用与 MCP 类似的注册模式——发现 SKILL.md → 生成 SkillToolHandler → registry.register() |
| → Phase 7 | 工具命名防冲突 | Resource/Prompt 的 `read_{server}_{name}` / `prompt_{server}_{name}` 命名模式可为 Skill 工具命名提供参考 |
| → Phase 8 | Cron + MCP | Scheduler cron 可触发 MCP 工具调用（如定时抓取外部数据） |
| → Phase 9 | MCP cancel 链路 | `_send_cancel()` 机制可与 CancelTokenRegistry 集成 |
| → 后续更新 | HttpClientTransport 路径 | stdio 传输的 `from mcp.client.stdio import StdioClientTransport` 和 streamable_http 的 `from mcp.client.http import HttpClientTransport` 导入路径与 mcp SDK 版本绑定，SDK 升级时需检查 |

### Phase 7：Skill 系统完善

**总体内容描述**

将 Skill 系统从"仅加载不注入"升级为完整的能力扩展机制：注入 system prompt、支持脚本执行、支持热加载、提供对话式创建向导、条件过滤。

**分模块内容描述**

| 模块 | 文件 | 描述 |
|------|------|------|
| Prompt 注入 | `agent/prompt/builder.py`（修改） | PromptBuilder 新增 `skills` section；`agent/prompt/providers.py` 激活 P3 预留的 `SkillsProvider.provide()`（从返回 None → 调用 SkillLoader） |
| 脚本执行 | `skills/loader.py`（修改） | SkillLoader 解析 SKILL.md 中的 `scripts/` 声明，注册为可执行工具 |
| 热加载 | `skills/loader.py`（修改） | 监测 `skills/` 目录变化，自动刷新技能列表 |
| 创建向导 | `skills/creator.py` | 对话式引导流程：收集元信息 → 生成 SKILL.md frontmatter → 写入 `skills/` 目录 |
| 条件过滤 | `skills/loader.py`（修改） | 解析 SKILL.md frontmatter 中的 `requires` 字段，过滤不可用技能 |

**分模块内容要点**

- Prompt 注入：`SkillLoader.build_skill_prompt()` 生成 `<available_skills>` XML 块，PromptBuilder 将其注入 system prompt 的 skills section
- 脚本执行：SKILL.md 中 `scripts/hello.py` 被解析为 `Skill.scripts: list[Path]`；Agent 可通过 `exec` 或 `python` 工具执行（不注册为独立工具，避免工具爆炸）
- 热加载：`SkillLoader` 新增 `watch()` 方法，使用 `watchfiles` 库（或轮询）监测目录变化，变化后自动 `reload()`
- 创建向导：`skill create` CLI 命令启动对话式流程。步骤：询问技能名称 → 描述 → 依赖（可选）→ 生成 SKILL.md 模板 → 写入 `skills/<name>/SKILL.md`
- 条件过滤：frontmatter 中的 `requires.binaries: ["ffmpeg"]` → `SkillLoader._check_requirements()` 检查 PATH → 不满足则放入 `unavailable_skills` 列表，注入 prompt 时显示为"不可用技能"

**开发注意事项**

- Skill 不直接注册为工具（避免工具列表膨胀），而是通过 instructions 文本引导 LLM 使用现有工具组合完成任务
- 热加载使用防抖（debounce 500ms），避免频繁保存触发多次重载
- 创建向导生成的 SKILL.md 使用标准 frontmatter 格式，与 P3 的 SkillLoader 兼容
- 条件过滤结果在 `/skills` 命令中展示（可用 + 不可用分开展示）
- **Phase 5 交付依赖**：`SkillToolProvider` 需实现 `tools/provider.py` 中定义的 `ToolProvider` ABC 接口；P3 预留的 `SkillsProvider` 骨架可通过 `SkillLoader.build_skill_prompt()` 激活注入；`ToolDefinition.source = ToolSource.SKILL` 枚举值已定义
- **Phase 6 交付参考**：`MCPToolProvider` 的编排模式（asyncio.gather 并行连接 + _clients/_pending/_failed 三层状态管理 + shutdown）可作为 `SkillToolProvider` 参考实现；Resource/Prompt 的 `read_{server}_{name}` / `prompt_{server}_{name}` 命名防冲突模式可借鉴

---

### Phase 8：Scheduler 增强

**总体内容描述**

将 Scheduler 从"仅一次性延迟提醒"升级为支持 cron 表达式、持久化存储、触发 Agent 处理的完整定时任务系统。

**分模块内容描述**

| 模块 | 文件 | 描述 |
|------|------|------|
| Cron 支持 | `scheduler/reminder.py`（重写） | 新增 `CronReminder` 类，使用 `croniter` 解析 cron 表达式，计算下次触发时间 |
| 持久化 | `scheduler/store.py` | 将定时任务持久化到 `data/scheduler.json`，重启不丢失 |
| Agent 触发 | `scheduler/reminder.py`（修改） | 定时任务触发时调用 `AgentLoop.run()`，通过 AgentContext 传入触发消息 |

**分模块内容要点**

- `CronReminder` 接口：`add_cron(name, cron_expr, prompt, session_id)` → `remove_cron(name)` → `list_crons()` → `start()` / `stop()`
- cron 表达式支持标准 5 字段格式（分 时 日 月 周），使用 `croniter` 库计算下次触发时间
- 持久化存储：`data/scheduler.json`，每次增删任务时写入。启动时从文件恢复所有任务
- Agent 触发：`CronReminder` 持有 `AgentLoop` 和 `channel` 引用，触发时调用 `agent.run(prompt)` 并处理异常
- CLI 新增：`/cron list`、`/cron add <cron> <prompt>`、`/cron remove <name>`

**开发注意事项**

- `croniter` 作为可选依赖（`pip install dotclaw[scheduler]`），未安装时 cron 功能不可用
- 持久化文件格式与 P1 的 session JSON 格式一致（`json.dumps(ensure_ascii=False, indent=2)`）
- Agent 触发模式下 `channel` 为 None，`ApprovalManager.check()` 默认放行（P1 已有逻辑）
- Scheduler 与 Agent 主循环运行在同一个 asyncio event loop 中，注意 cron 触发不影响活跃对话

---

### Phase 9：取消机制

**总体内容描述**

实现 Agent 请求取消能力：CancelTokenRegistry 管理进程内取消令牌，在 Agent 循环中插入安全检查点，CLI 提供 `/cancel` 命令。

**分模块内容描述**

| 模块 | 文件 | 描述 |
|------|------|------|
| CancelTokenRegistry | `agent/cancel.py` | 进程内取消令牌注册表，支持按 request_id 或 session_id 注册/取消 |
| CancelToken | `agent/cancel.py` | 取消令牌对象：`is_cancelled()` 检查、`cancel()` 触发 |
| 安全检查点 | `agent/loop.py`（修改） | AgentLoop 在轮次边界、工具执行前后、LLM 流式块之间调用 `_check_cancelled()` |
| CLI 命令 | `main.py`（修改） | 新增 `/cancel` 命令和快捷键（Ctrl+C）处理 |

**分模块内容要点**

- `CancelTokenRegistry` 接口：`register(request_id, session_id)` → `cancel(request_id)` → `cancel_session(session_id)` → `is_cancelled(token)`
- `CancelToken` 是 `threading.Event` 的 asyncio 封装，支持同步/异步双重检查
- 安全检查点位置：每次 `for` 循环迭代开始、每个工具执行前后、每个 LLM 流式 chunk 之间
- 取消响应：`_check_cancelled()` 抛出 `AgentCancelledError`，在 `run()` 的 finally 块中处理（保存已生成的部分回复到 session）
- 命令行：`Ctrl+C` 发送取消信号（而非退出）；第二次 `Ctrl+C` 强制退出。`/cancel` 命令取消当前活跃对话的 Agent 请求

**开发注意事项**

- 取消是"尽力而为"的：工具执行中（如 `exec` 长时间运行的命令）可能无法立即中断，需等待工具返回后再响应取消
- `CancelTokenRegistry` 使用进程内存存储（不持久化），会话结束自动清理
- `AgentCancelledError` 需在 `main.py` 的命令循环中捕获，确保取消后用户仍可继续输入
- 与 Scheduler 的交互：Scheduler 触发的 Agent 请求也可以通过 `/cancel` 取消

---

### Phase 10：测试 + 多渠道（远期）

**总体内容描述**

提升代码质量和测试覆盖率，增加 Web UI 通道支持。

**分模块内容描述**

| 模块 | 描述 |
|------|------|
| 单元测试 | 覆盖 LLM 层、工具系统、记忆系统、Agent 循环的核心函数 |
| 集成测试 | 多轮对话 + 工具调用完整流程，使用 MockLLM 和真实 ToolRegistry |
| 端到端测试 | CLI 自动交互测试，使用 pexpect 或 subprocess |
| 稳定性 | API 限流处理、网络异常重试、JSON 解析失败降级、超时保护 |
| Web Channel | SSE 流式 Web UI，复用现有 Channel 抽象基类 |

**分模块内容要点**

- 测试框架：pytest + pytest-asyncio。Mock 对象：`MockLLMClient`（返回预设 chunk 序列）、`FakeChannel`（内存 channel，P1 已有基础版本）
- 测试覆盖率目标：核心模块 ≥ 80%，工具模块 ≥ 70%
- Web Channel：`WebChannel(Channel)` 实现，FastAPI + SSE 流式响应。前端用简单的 HTML + HTMX 或 Web Component
- 稳定性增强：`LLMProxy` 新增指数退避上限控制、429 状态码特殊处理。`ToolRegistry.execute()` 新增超时配置

**开发注意事项**

- 测试编写随各阶段进行，不在 P10 集中补课（P2-P9 每一步完成后即写对应测试）
- Web Channel 不影响现有 CLI 逻辑，通过 `ChannelManager` 统一管理
- 多渠道优先级：CLI > Web UI > IM Bot。P10 仅做 Web UI

---
