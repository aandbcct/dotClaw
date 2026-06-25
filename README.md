<div align="center">

# 🐾 dotClaw

**轻量级开源 AI 个人助手框架**

ReAct推理循环 · 多模型路由降级 · 上下文感知注入 · 三级记忆 · 可插拔工具与技能

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/ttguy0707/dotClaw?color=orange)](https://github.com/aandbcct/dotClaw)

</div>

---

> **dotClaw** — 面向个人助手场景的轻量级 Agent 框架。**实现ReAct循环、工具调度、记忆管理和模型路由**的完整链路。
> 以**三层记忆分级为内核**，Skill 按需注入为扩展机制，在有限上下文窗口内最大化长期关系维护能力。

---

## ✨ 核心特性

| 特性 | 说明                                                          |
|------|-------------------------------------------------------------|
| **ReAct 推理循环** | 思考→行动→观察的推理循环，让agent逐步思考，动态响应复杂任务                           |
| **链路白盒化**     | 16类事件覆盖5大模块，三类统计结果支持请求全链路追溯                                 |
| **多模型路由** | 多供应商适配（Qwen / DeepSeek / OpenAI），优先级自动选择 + 跨供应商降级           |
| **三层记忆分级** | L1 工作记忆（当前会话）/ L2 短期记忆（日记忆）/ L3 长期记忆（DeepDream 蒸馏），跨会话记忆不丢失 |
| **上下文感知注入** | PromptBuilder 多 Provider 分层构建，记忆按时效性/相关性排序注入，token 预算精细分配   |
| **Skill 按需激活** | 扫描注册 + prompt 注入，根据对话场景动态激活相关技能                             |
| **可插拔工具** | ToolHandler/Registry/Executor 三层架构 + 8 个内置工具 + 审批机制         |
| **MCP 协议** | stdio + Streamable HTTP 双传输，无缝接入外部工具服务                      |
| **全链路观测** | 16 类事件覆盖 5 大模块，trace / report / snapshot 三路输出 |

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
│   ├── agent/               # Agent 核心：ReAct 循环 + PromptBuilder + 工厂
│   ├── journal/             # 统一观测：16 种事件 / 三路输出 / Snapshot
│   ├── llm/                 # LLM 客户端：多供应商适配 + ModelRouter
│   ├── tools/               # 工具系统：三层架构 + 审批 + SkillParser
│   ├── mcp/                 # MCP 协议：stdio + Streamable HTTP 双传输
│   ├── skills/              # Skill 系统：扫描注册 + prompt 注入
│   ├── memory/              # 三层记忆：L0/L1/L2 + DeepDream 蒸馏
│   ├── channel/             # 通道（CLI）
│   ├── scheduler/           # 定时提醒
│   ├── common/              # 通用工具库（限流器等）
│   └── config/              # 配置加载
├── benchmarks/              # 评测系统
│   ├── runner.py            # 评测总控
│   ├── cases/               # 6 个评测用例
│   ├── dataset/             # 评测数据集
│   └── baselines/           # 基线快照
├── tests/                   # 验收测试
├── skills/                  # 技能目录
├── data/                    # 运行时数据
├── config.yaml              # 配置文件
└── model_router_config.yaml # 模型路由配置
```

### 核心模块

| 模块 | 路径 | 职责 |
|------|------|------|
| **Agent 循环** | `agent/` | ReAct 推理循环 + AgentContext + PromptBuilder + 工厂装配 |
| **三层记忆** | `memory/` | L0 工作记忆 / L1 日记忆（FTS5+向量双路） / L2 蒸馏（DeepDream） |
| **统一观测** | `journal/` | 16 种事件覆盖 5 大域，trace.jsonl / report.json / snapshot.json |
| **工具系统** | `tools/` | Handler/Registry/Executor 三层 + 审批机制 + SkillParser + SkillResolver |
| **LLM 客户端** | `llm/` | Qwen/DeepSeek/OpenAI 适配，ModelRouter 优先级路由 + 降级 |
| **MCP 协议** | `mcp/` | stdio + Streamable HTTP，McpClient 状态机 |
| **Skill 系统** | `skills/` | 扫描注册 + prompt 注入 + SkillsProvider 场景感知激活 |
| **评测系统** | `benchmarks/` | 6 维评测 + 基线对比 + AgentRunSnapshot + Markdown 报告 |
| **配置管理** | `config/` | YAML → dataclass，类型安全的配置加载 + 环境变量展开 |

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

6 维框架性能评测，支持 warmup、P50/P95 统计、基线对比。

```bash
# 运行全部评测
python -m benchmarks.runner

# 指定 case
python -m benchmarks.runner --filter init_perf,tool_dispatch

# 调节参数
python -m benchmarks.runner --warmup 3 --repeat 10

# 基线对比
python -m benchmarks.runner --baseline benchmarks/baselines/v1.0/init_perf.json
```

| 评测维度 | 说明 | 核心指标 |
|----------|------|----------|
| `init_perf` | 各组件初始化耗时 | Config / LLM / Skill / Tool / Memory / Agent 全装配 P95 |
| `tool_dispatch` | 工具调度纯开销 | no-op handler P95（排除工具执行时间） |
| `llm_stream` | LLM 流式延迟 [EXT] | TTFT / TPS / E2E（含网络延迟） |
| `memory_perf` | 记忆检索性能 | FTS5 在 100/1K/10K chunks 下的 P50/P95 |
| `skill_load` | Skill 加载性能 | 10/50/100 skills 扫描+注册耗时 |
| `stress` | 压力测试 | 50 并发工具 / 50KB+ 大上下文 / 20 步 ReAct |

---

## 🧪 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行验收测试
python tests/test_phase1_acceptance.py   # ReAct 循环 + 工具系统
python tests/test_phase2_acceptance.py   # 多供应商路由
python tests/test_phase3_acceptance.py   # AgentContext + PromptBuilder
python tests/test_phase4_acceptance.py   # 三级记忆
python tests/test_phase5_acceptance.py   # 工具层架构重构
python tests/test_phase7_acceptance.py   # Skill 系统
python tests/test_phase8_acceptance.py   # Journal 统一观测
```

---

## 📊 开发进度

| Phase | 状态 | 说明 |
|:-----:|:----:|------|
| Phase 1 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | ReAct 循环、工具系统、流式输出、审批、调试追踪 |
| Phase 2 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | 多供应商路由、OpenAICompatibleClient 基类、priority 降级、限流器 |
| Phase 3 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | AgentContext、PromptBuilder、AgentResult、message_utils |
| Phase 4 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | SQLite FTS5 混合检索、LLM 日记忆摘要、DeepDream 蒸馏、MemoryProvider |
| Phase 5 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | 工具层架构重构：Handler/Registry/Executor 三层分离、去硬编码审批 |
| Phase 6 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | MCP 协议集成：双传输 + McpClient 状态机 + MCPToolProvider |
| Phase 7 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | Skill 系统：扫描注册 + prompt 注入 + SkillsProvider + SkillResolver |
| Phase 8 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | Journal 统一观测：16 种事件 / 三路输出 / trace + report + snapshot |
| Phase 9 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | 评测系统：6 维评测 + 基线对比 + AgentRunSnapshot + Markdown 报告 |
| Phase 10 | ![搁置](https://img.shields.io/badge/⏸️-搁置-yellow) | Scheduler cron 增强 |
| Phase 11 | ![搁置](https://img.shields.io/badge/⏸️-搁置-yellow) | 取消机制（CancelTokenRegistry） |
| Phase 12 | ![搁置](https://img.shields.io/badge/⏸️-搁置-yellow) | Web Channel |

---

## 🗺️ 后续开发计划

| 模块 | 完成情况  | 说明 |
|:----:|:-----:|------|
| **上下文工程** | 已完成 | 重构会话上下文构成：token 预算分配、记忆时效性衰减、Skill 按相关性截断 |
| **记忆系统深化** | 未完成 | L2 召回优先级优化、记忆冲突处理、L1→L2 蒸馏策略细化 |
| **工具安全护栏** | 未完成 | 工具调用前置校验、参数沙箱、命令分级审批（读放行/写审批/危险阻断） |
| **个人助手工具** | 未完成 | 日历/提醒/Todo 工具接入（MCP 或 builtin） |
| **主动推送** | 未完成 | 定时触发 + 推送通道，agent 主动提醒而非被动应答 |
| **测试覆盖率** | 未完成 | 单元测试 + 集成测试覆盖核心链路 |

| 维度       | dotClaw 现状 | 主流框架标配                 |
| :--------- | :----------- | :--------------------------- |
| 执行可靠性 | 三阶段容错   | Checkpoint + 断点续跑 + HITL |
| 输出稳定性 | 未明确       | Structured Output + 自动修复 |
| 效果验证   | 运行工件监控 | 离线 Eval + 量化对比         |
| 架构扩展性 | 单体个人助手 | 子Agent路由 / 多智能体拓扑   |
| 交互体验   | 未提及       | 流式输出 + 中间步骤透传      |

---

## 📄 License

[MIT](LICENSE)

---

<div align="center">

**🐾 dotClaw · 轻量级开源 AI 个人助手框架**

</div>
