<div align="center">

# 🐾 dotClaw

**轻量级 Agent harness 框架**

ReAct推理循环 · 多模型路由降级 · 上下文感知注入 · 三级记忆 · 可插拔工具与技能

[![Python](https://img.shields.io/badge/Python-3.13+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/aandbcct/dotClaw?color=orange)](https://github.com/aandbcct/dotClaw)

</div>

---

> **dotClaw** — 轻量级 Agent harness框架，v0.5 采用 **Identity × Runtime 解耦架构**。
> Identity 定义角色约束（白名单/模型/行为），Runtime 纯执行引擎，通过 AgentRun 原子调用串联。
> 以**三层记忆分级为内核**，Skill 按需注入为扩展机制，在有限上下文窗口内最大化长期关系维护能力。

---

## ✨ 核心特性

| 特性                | 说明                                                                        |
|-------------------|---------------------------------------------------------------------------|
| **agent 解耦**      | Agent = AgentIdentity（agent声明）+ AgentRuntime（执行引擎），权限白名单/行为模板/模型选择零耦合     |
| **AgentRun 原子调用** | 一次 LLM 上下文窗口占用 = 一个 AgentRun，跨 Run 状态通过 AgentState 累加，支持 tick-level 中断恢复  |
| **ReAct 推理循环**    | 思考→行动→观察的推理循环，让 agent 逐步思考，动态响应复杂任务                                       |
| **三层记忆分级**        | L0 工作记忆 / L1 日记忆（FTS5+向量双路召回） / L2 DeepDream 蒸馏，嵌入向量接入 LLM 统一管理           |
| **上下文感知注入**       | ContextSlot 四级分层组装（Static→Session→Conditional→Dynamic），token 预算精细分配       |
| **多模型路由**    | Proxy 重试编排 + Router 智能选路（含限流/熔断/降级），CircuitBreaker 按 provider 自动熔断恢复      |
| **全链路观测**         | Journal 16 类事件 + history.jsonl / state.json / trace.jsonl 原子I/O写入，链路完全白盒化 |
| **Skill 按需激活**    | 扫描注册 + prompt 注入，SkillsProvider 根据对话场景动态激活                                |
| **可插拔工具**         | Handler/Registry/Executor 三层架构 + 8 个内置工具 + 审批机制                           |
| **MCP 协议**        | stdio + Streamable HTTP 双传输，McpClient 状态机 + ExternalToolProvider          |

---

## 🚀 快速开始

### 1️⃣ 安装

```bash
pip install -e .
```

### 2️⃣ 配置

```bash
# 设置 API Key
export QWEN_API_KEY=sk-xxxxxxxxxxxxxxxx

# 或编辑 config.yaml 配置模型、路由等参数
```

### 3️⃣ 启动

```bash
python -m dotclaw
# 或
dotclaw
```

---

## 🧠 三层记忆系统

dotClaw 的记忆系统是个人助手场景的核心——agent 需要跨会话记住用户偏好、习惯、关系和决策历史。

```
┌─────────────────────────────────────────────────┐
│  L0 工作记忆 (Working Memory)                    │
│  当前会话的上下文窗口，会话结束即清空                  │
├─────────────────────────────────────────────────┤
│  L1 短期记忆 (Short-term Memory)                 │
│  日记忆摘要，SQLite FTS5 + 向量双路召回             │
│  保留近期细节，过期后触发 DeepDream 蒸馏             │
├─────────────────────────────────────────────────┤
│  L2 长期记忆 (Long-term Memory)                  │
│  DeepDream 蒸馏后的偏好、关系、决策模式              │
│  跨会话持久化，新会话靠 L2 召回重建用户认知            │
└─────────────────────────────────────────────────┘
```

**核心设计决策：**

- **蒸馏触发**：L1 记忆过期时自动触发 DeepDream，将细节压缩为 L2 蒸馏记忆
- **召回优先级**：时效性衰减 + 亲密度权重 + 相关性排序，有限 token 内注入最有价值的记忆
- **记忆冲突**：新旧偏好矛盾时，以时间近度和置信度加权决策

---

## 🏗️ 项目结构

```
dotClaw/
├── src/dotclaw/             # 源代码
│   ├── agent/               # Agent 核心：Identity + Runtime + ContextSlot + 中断恢复
│   ├── session/             # 会话体系：Session + AgentRun + AgentState
│   ├── journal/             # 统一观测：16 种事件 + history/state/trace 三路 sink
│   ├── llm/                 # LLM 引擎：Proxy + Router + CircuitBreaker + RateLimiter
│   ├── tools/               # 工具系统：Handler/Registry/Executor + 审批 + SkillParser
│   ├── mcp/                 # MCP 协议：stdio + Streamable HTTP 双传输
│   ├── skills/              # Skill 系统：扫描注册 + prompt 注入
│   ├── memory/              # 三层记忆：FTS5+向量双路召回 + DeepDream + Embedding
│   ├── channel/             # 通道（CLI，Rich 渲染）
│   ├── cli/                 # CLI Banner（dotClaw ASCII art）
│   ├── scheduler/           # 定时提醒
│   ├── common/              # 通用工具函数
│   └── config/              # 配置加载（YAML → dataclass）
├── Eval/                    # 评测系统
│   ├── runner.py            # 评测总控
│   ├── cases/               # 6 个评测用例
│   ├── dataset/             # 评测数据集
│   └── baselines/           # 基线快照
├── tests/                   # 单元测试 + 验收测试
│   ├── agent/               # Identity / Runtime / Resume
│   ├── context/             # ContextSlot / Slots
│   ├── journal/             # Journal 事件 + history/state
│   ├── metrics/             # Snapshot 指标计算
│   └── session/             # AgentRun
├── skills/                  # 技能目录
├── data/                    # 运行时数据
├── config.yaml              # 配置文件
└── model_router_config.yaml # 模型路由配置
```

### 核心模块

| 模块 | 路径 | 职责 |
|------|------|------|
| **Agent 双核** | `agent/` | AgentIdentity（声明式约束）+ AgentRuntime（纯执行引擎），白名单权限/行为模板零耦合 |
| **会话体系** | `session/` | Session（对话隔离）+ AgentRun（原子调用记录）+ AgentState（跨 Run 累加器），支持 tick 级恢复 |
| **上下文工程** | `agent/slotContext.py` | Tier 分层 Slot 插拔：Static → Session → Conditional → Dynamic 四级组装 |
| **三层记忆** | `memory/` | L0 工作记忆 / L1 日记忆（FTS5+向量双路召回）/ L2 DeepDream 蒸馏 + Embedding 缓存 |
| **LLM 引擎** | `llm/` | Proxy（重试编排）+ Router（智能选路+限流熔断）+ CircuitBreaker + RateLimiter |
| **统一观测** | `journal/` | 16 种事件 + history.jsonl / state.json / trace.jsonl 原子写入 |
| **工具系统** | `tools/` | Handler/Registry/Executor 三层 + 审批机制 + SkillParser |
| **MCP 协议** | `mcp/` | stdio + Streamable HTTP 双传输，McpClient 状态机 + ExternalToolProvider |
| **Skill 系统** | `skills/` | 扫描注册 + prompt 注入 + SkillsProvider 按需激活 |
| **评测系统** | `Eval/` | 6 维评测 + 基线对比 + AgentRunSnapshot + Markdown 报告 + warmup/P50/P95 |
| **配置管理** | `config/` | YAML → dataclass 类型安全加载 + 环境变量 ${ENV} 展开 + Agent YAML 配置 |

---

## 💻 CLI 命令

### 对话管理

| 命令 | 说明 |
|------|------|
| `/new [标题]` | 新建对话 |
| `/list` | 列出所有对话 |
| `/switch <id>` | 切换到指定对话 |
| `/delete <id>` | 删除对话 |

### 工具与调试

| 命令 | 说明 |
|------|------|
| `/tools` | 列出所有可用工具（按来源分组：BUILTIN / MCP） |
| `/mcp` | 查看 MCP servers 连接状态 |
| `/skills` | 列出已加载技能 |
| `/debug` | 查看最近一次推理过程 |

### 系统配置

| 命令 | 说明 |
|------|------|
| `/model <名称>` | 切换模型（支持跨供应商） |
| `/help` | 显示帮助 |
| `/quit` | 退出 |

---

## 🔀 多供应商路由

通过 `model_router_config.yaml` 配置多供应商路由：

- **Qwen**：`qwen3.7-max`、`qwen-turbo`
- **DeepSeek**：`deepseek-v3`、`deepseek-v4-flash`
- **OpenAI**：`gpt-4o-mini`

**路由规则**：`purposes.chat.priority` 中 priority 数值最小的 active 模型优先使用；失败时按优先级顺序依次降级。

---

## 📊 评测系统

6 维框架性能评测，支持 warmup、P50/P95 统计、多版本基线对比。

```bash
# 运行全部评测
python -m Eval.runner

# 指定 case
python -m Eval.runner --filter init_perf,tool_dispatch

# 调节参数
python -m Eval.runner --warmup 3 --repeat 10

# 保存基线
python -m Eval.runner --save-baseline Eval/baselines/v1.0

# 基线对比
python -m Eval.runner --baseline Eval/baselines/v1.0
```

| 评测维度 | 说明 | P50/P95 核心统计 |
|----------|------|-------------------|
| `init_perf` | 各组件初始化耗时 | Config / Identity / Runtime / Skill / Tool / Memory / Agent 全装配 |
| `tool_dispatch` | 工具调度纯开销 | no-op handler 调度延迟（排除工具执行时间） |
| `llm_stream` | LLM 流式延迟 [EXT] | TTFT / TPS / E2E（含网络延迟） |
| `memory_perf` | 记忆检索性能 | FTS5 在 100/1K/10K chunks 下的检索延迟 |
| `skill_load` | Skill 加载性能 | 10/50/100 skills 扫描+注册耗时 |
| `stress` | 压力测试 | 50 并发工具 / 50KB+ 大上下文 / 20 步 ReAct |

---

## 🧪 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# Phase 验收测试
python tests/test_phase1_acceptance.py   # ReAct 循环 + 工具系统
python tests/test_phase2_acceptance.py   # 多供应商路由
python tests/test_phase3_acceptance.py   # AgentContext + PromptBuilder
python tests/test_phase4_acceptance.py   # 三级记忆
python tests/test_phase7_acceptance.py   # Skill 系统

# 模块单元测试
pytest tests/agent/ -v                    # Identity / Runtime / Resume
pytest tests/context/ -v                  # ContextSlot 上下文工程
pytest tests/journal/ -v                  # Journal 事件 + history/state
pytest tests/metrics/ -v                  # Snapshot 指标计算
pytest tests/session/ -v                  # AgentRun / AgentState
```

---

## 📊 开发进度

| Phase | 状态 | 说明 |
|:-----:|:----:|------|
| Phase 1 | ![✅](https://img.shields.io/badge/✅-完成-brightgreen) | ReAct 循环、工具系统、流式输出、审批、调试追踪 |
| Phase 2 | ![✅](https://img.shields.io/badge/✅-完成-brightgreen) | 多供应商路由、OpenAICompatibleClient 基类、priority 降级 |
| Phase 3 | ![✅](https://img.shields.io/badge/✅-完成-brightgreen) | AgentContext、PromptBuilder、message_utils |
| Phase 4 | ![✅](https://img.shields.io/badge/✅-完成-brightgreen) | SQLite FTS5 向量混合检索、LLM 日记忆摘要、DeepDream 蒸馏 |
| Phase 5 | ![✅](https://img.shields.io/badge/✅-完成-brightgreen) | 工具层架构重构：Handler/Registry/Executor 三层分离 + 去硬编码审批 |
| Phase 6 | ![✅](https://img.shields.io/badge/✅-完成-brightgreen) | MCP 协议集成：双传输 + McpClient 状态机 + ExternalToolProvider |
| Phase 7 | ![✅](https://img.shields.io/badge/✅-完成-brightgreen) | Skill 系统：扫描注册 + prompt 注入 + SkillsProvider + SkillResolver |
| Phase 8 | ![✅](https://img.shields.io/badge/✅-完成-brightgreen) | Journal 统一观测：16 种事件 + history/state/trace 原子写入 |
| Phase 9 | ![✅](https://img.shields.io/badge/✅-完成-brightgreen) | 评测系统（Eval）：6 维评测 + warmup/P50/P95 + 基线对比 + Snapshot |
| Phase 10 | ![✅](https://img.shields.io/badge/✅-完成-brightgreen) | Agent/Session 解耦：Identity × Runtime 架构 + AgentRun/AgentState + CircuitBreaker |
| Phase 11 | ![⏸️](https://img.shields.io/badge/⏸️-搁置-yellow) | 数据统计：趋势分析 + 持续监控 + 可视化 |
| Phase 12 | ![⏸️](https://img.shields.io/badge/⏸️-搁置-yellow) | 取消机制（CancelTokenRegistry） |
| Phase 13 | ![⏸️](https://img.shields.io/badge/⏸️-搁置-yellow) | Web Channel |

---

## 🗺️ 后续开发计划

| 模块 | 状态 | 说明 |
|:----:|:----:|------|
| **多 Agent 路由** | 未完成 | AgentRun 支持 Agent 间 handoff，子 Agent spawn + 结果回传 |
| **工具安全护栏** | 未完成 | 工具调用前置校验、参数沙箱、命令分级审批（读放行/写审批/危险阻断） |
| **记忆系统深化** | 未完成 | L2 召回优先级优化、记忆冲突处理、L1→L2 蒸馏策略细化 |
| **个人助手工具** | 未完成 | 日历/提醒/Todo 工具接入（MCP 或 builtin） |
| **主动推送** | 未完成 | 定时触发 + 推送通道，agent 主动提醒而非被动应答 |
| **测试覆盖率** | 未完成 | 端到端集成测试覆盖核心链路 |

| 维度 | dotClaw v0.5 现状 | 下一阶段目标 |
|:-----|:-------------------|:-------------|
| 架构扩展性 | Identity × Runtime 解耦，AgentRun 原子调用 | 子 Agent 路由 / 多 Agent 拓扑 |
| 执行可靠性 | AgentState 累加 + history/state 原子恢复 | Checkpoint + 断点续跑 + HITL |
| LLM 容错 | 多供应商路由 + CircuitBreaker + 降级 | Structured Output + 自动修复 |
| 效果验证 | Eval 6 维评测 + P50/P95 + 基线对比 | 趋势分析 + 持续监控 + 可视化 |
| 交互体验 | Rich CLI + 流式输出 | Web Channel |

---

## 📄 License

[MIT](LICENSE)

---

<div align="center">

**🐾 dotClaw v0.5 · Identity × Runtime 解耦 Agent 框架**

</div>
