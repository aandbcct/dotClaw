# dotClaw 项目架构分析与开发路线图

更新时间：2026-06-01（含 CowAgent 对比分析）

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
│   ├── logger.py        ← AgentLogger 结构化日志 + request_id（Phase 3 ✅）
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
├── tools/               ✅ 已完成
│   ├── __init__.py      ← 自动注册所有工具模块
│   ├── base.py          ← 注册表：@register_tool + ToolRegistry
│   ├── approval.py      ← 危险工具审批
│   ├── exec_tool.py     ← Shell 执行
│   ├── file_tool.py     ← read_file / write_file / list_dir
│   ├── memory_tool.py   ← memory_read / memory_write
│   ├── system_tool.py   ← system_info / get_time
│   ├── python_tool.py   ← 📋 规划：Python 代码执行（Phase 5）
│   ├── web_search.py    ← 📋 规划：网络搜索（Phase 5）
│   └── web_fetch.py     ← 📋 规划：URL 内容获取（Phase 5）
│
├── skills/              ⚠️ 仅 loader.py 完成
│   └── loader.py        ✅ SKILL.md 解析加载
│
├── mcp/                 ← 📋 规划：MCP 协议客户端（Phase 6）
│
├── scheduler/           ⚠️ 最简版本
│   └── reminder.py      ✅ 一次性提醒
│
├── debug/               ✅ 已完成
│   └── logger.py        ← TraceRecord + DebugManager
│
├── model_router_config.yaml  ← 路由配置（providers/models/purposes 三层）
│
└── tests/               ✅ Phase 1 + 2 + 3 + 4 测试
    ├── test_phase1_acceptance.py
    ├── test_phase2_acceptance.py
    ├── test_phase3_acceptance.py
    └── test_phase4_acceptance.py
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
| 工具系统 | ✅ 完整 | 8 个工具自动注册；审批从 config 读取 |
| DebugManager | ✅ 完成 | TraceRecord + /debug 命令（P3 由 AgentLogger 回写） |
| 测试 | ✅ 完成 | P1: 7/7 + P2: 7/7 + P3: 8/8 + P4: 6/6 全部通过 |
| Skill 加载 | ⚠️ 半成品 | 能加载 SKILL.md，未注入 system prompt |
| 定时提醒 | ⚠️ 最简版 | 仅一次性延迟提醒 |
| MCP 集成 | ❌ 未实现 | 不支持 MCP 协议 |
| python 工具 | ❌ 未注册 | 审批引用了但未注册 |

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
4. **工具失败恢复机制**：相同参数连续失败/循环检测、JSON 参数修复 → Phase 5
5. **条件性工具加载**：根据环境变量/依赖自动决定工具是否可用 → Phase 5
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

> 详细开发文档：[docs/phase2-roadmap.md](./phase2-roadmap.md)

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

> 详细开发文档：[docs/phase3-roadmap.md](./phase3-roadmap.md)

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

> 详细开发文档：[docs/phase4-roadmap.md](./phase4-roadmap.md) | 变更日志：[docs/phase4-record.md](./phase4-record.md)

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

### Phase 5：工具系统增强

**总体内容描述**

新增 python、web_search、web_fetch 三个工具，实现条件性工具加载机制和工具失败恢复机制。目标是增强工具系统的功能性和健壮性。

**分模块内容描述**

| 模块 | 文件 | 描述 |
|------|------|------|
| python 工具 | `tools/python_tool.py` | 执行 Python 代码片段，通过 subprocess 隔离执行，需审批 |
| web_search 工具 | `tools/web_search.py` | 网络搜索，通过 API 调用搜索引擎，条件加载（需 API key） |
| web_fetch 工具 | `tools/web_fetch.py` | 获取 URL 内容并提取纯文本，支持常见编码和重定向 |
| 条件加载 | `tools/base.py`（修改） | `ToolRegistry` 新增条件加载逻辑：工具函数声明 `requires` 字段，启动时检查 API key/依赖 |
| 失败恢复 | `agent/loop.py`（修改） | `AgentLoop.run()` 中新增工具调用追踪：相同参数连续失败 N 次 → 停止；相同参数连续调用 M 次 → 提示 LLM |
| 日志合并 | `debug/logger.py`（删除） | 将 DebugManager 的 TraceRecord 能力合并到 AgentLogger，删除 `debug/logger.py`（P3 遗留技术债） |

**分模块内容要点**

- `python` 工具：通过 `asyncio.create_subprocess_exec` 隔离执行，超时 30s，需 ApprovalManager 审批（与 exec 工具一致）
- `web_search` 工具：使用 `@register_tool` 装饰器新增 `requires={"env": "WEB_SEARCH_API_KEY"}` 声明；`ToolRegistry._check_requirements()` 在注册时检查，不满足则跳过
- `web_fetch` 工具：使用 aiohttp 或 httpx 获取 URL，`BeautifulSoup` 或正则提取文本，支持 3xx 重定向、超时 10s
- 条件加载：`ToolDefinition` 新增 `requires: dict` 字段（env vars / binaries / Python packages）。`ToolRegistry.__init__()` 调用 `_check_requirements()` 过滤
- 失败恢复：`AgentLoop` 维护 `tool_failure_history: dict[str, list]`，记录最近 50 次工具调用。检测规则：相同 (name, args) 连续失败 ≥ 3 → 停止并告知 LLM；相同 (name, args) 连续成功 ≥ 5 → 提示 LLM 可能陷入循环
- 日志合并（P3 遗留技术债）：将 `debug/logger.py` 的 `TraceRecord` + `DebugManager` 能力合并到 `AgentLogger`，删除 `debug/logger.py`。`AgentLogger` 直接管理 `_last_trace`，`/debug` 命令从 `AgentLogger.get_last_trace()` 读取

**开发注意事项**

- `python` 工具需严格沙箱：禁用网络访问、限制文件系统访问范围（`config.tools.python_sandbox`）
- `web_search` 的 API 对接按供应商抽象接口（先实现一个供应商，后续扩展类似 LLM 模式）
- 条件加载的 `requires` 检查仅影响注册，不影响已注册工具的热重载
- 失败恢复机制在 P1 已有的 `max_iterations` 基础上增加细粒度保护
- **日志合并**：P5 完成后 `debug/logger.py` 应被删除，`AgentLoop` 不再持有两个 logger 实例。合并时注意 `/debug` 命令的兼容性（当前通过 DebugManager 回写实现）

---

### Phase 6：MCP 协议集成

**总体内容描述**

实现 MCP（Model Context Protocol）客户端，支持通过 MCP 协议动态注册外部工具，并提供增量热重载能力。

**分模块内容描述**

| 模块 | 文件 | 描述 |
|------|------|------|
| MCP 客户端 | `mcp/client.py` | 支持 stdio 和 SSE 两种传输方式的 MCP 客户端实现 |
| 工具适配器 | `mcp/tool_adapter.py` | 将 MCP 工具定义转换为 dotClaw `ToolDefinition` 格式 |
| 配置管理 | `mcp/config.py` | 解析 `config.yaml` 中 `mcp_servers` 配置，启动/停止服务器进程 |
| 热重载 | `mcp/watcher.py` | 监测 `config.yaml` 变化，差异式增量更新工具列表 |

**分模块内容要点**

- MCP 客户端遵循 MCP 协议规范：`initialize` → `tools/list` → 注册工具 → `tools/call`
- stdio 传输：通过 `asyncio.create_subprocess_exec` 启动 MCP 服务器子进程，JSON-RPC 通过 stdin/stdout 通信
- SSE 传输：通过 HTTP SSE 连接远程 MCP 服务器，支持重连
- 工具适配器将 MCP 的 `inputSchema` 映射为 dotClaw 的 `ToolDefinition.parameters`（JSON Schema 格式）
- 热重载：每 30s 检查 `config.yaml` 的修改时间 + SHA256 签名，差异式处理（新增/删除/重启变化的服务器）。不变的服务器不重启

**开发注意事项**

- MCP 服务器加载使用后台 asyncio task，不阻塞 Agent 首次消息响应
- 单个 MCP 服务器崩溃不影响其他服务器和主流程（try/except 隔离 + 日志记录）
- `mcp_servers` 配置格式：`[{name, transport: "stdio"|"sse", command?, args?, url?}]`
- MCP 工具通过 `ToolRegistry.register()` 动态注册，与内置工具（exec、read_file 等）放在同一命名空间，名称冲突时内置优先

---

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
