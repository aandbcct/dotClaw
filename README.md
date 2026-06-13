<div align="center">

# 🐾 dotClaw

**轻量级开源 Agent Harness 项目**

ReAct推理循环 · 多模型路由降级 · 三级记忆 · 可插拔工具与技能

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/ttguy0707/dotClaw?color=orange)](https://github.com/aandbcct/dotClaw)

</div>

---

> **dotClaw** — 一个从零搭建的 Agent Harness 项目。**实现ReAct循环、工具调度、记忆管理和模型路由**的完整链路。
> 

---

## ✨ 核心特性

| 特性            | 说明                                               |
|---------------|--------------------------------------------------|
| **ReAct推理循环** | 思考→行动→观察的推理循环，让agent逐步思考，动态响应复杂任务                |
| **链路白盒化**     | 16类事件覆盖5大模块，三类统计结果支持请求全链路追溯                      |
| **多模型路由**     | 多供应商适配（Qwen / DeepSeek / OpenAI），优先级自动选择 + 跨供应商降级 |
| **三级记忆**      | L1 Session / L2 日记忆 / L3 蒸馏，上下文管理不失控             |
| **可插拔工具**     | ToolHandler/Registry/Executor 三层架构 + 8 个内置工具 + 审批机制 |
| **MCP 协议**    | stdio + Streamable HTTP 双传输，无缝接入外部工具服务           |
| **Skill 系统**  | 扫描注册 + prompt 注入，技能即插即用                          |

---

## 🚀 快速开始

### 1️⃣ 安装

```bash
pip install -e .
```

### 2️⃣ 配置

```bash
# 设置 API Key
export DOTCLAW_API_KEY=sk-xxxxxxxxxxxxxxxx

# 或编辑 config.yaml 配置模型、路由等参数
```

### 3️⃣ 启动

```bash
python -m dotclaw
# 或
dotclaw
```

---

## 🏗️ 项目结构

```
dotClaw/
├── src/dotclaw/             # 源代码
│   ├── agent/               # Agent 核心循环
│   ├── journal/              # 统一观测模块
│   ├── llm/                 # LLM 客户端
│   ├── tools/               # 工具系统
│   ├── mcp/                 # MCP 协议客户端
│   ├── skills/              # Skill 系统
│   ├── memory/              # 三级记忆系统
│   ├── channel/             # 通道（CLI）
│   ├── scheduler/           # 定时提醒
│   ├── common/              # 通用工具库
│   └── config/              # 配置加载
├── tests/                   # 测试
├── skills/                  # 技能目录
├── data/                    # 运行时数据
├── config.yaml              # 配置文件
└── model_router_config.yaml # 模型路由配置
```

### 核心模块

| 模块 | 路径 | 职责 |
|------|------|------|
| **Agent 循环** | `agent/` | ReAct 推理循环 + AgentContext 上下文 + PromptBuilder 提示构建 |
| **统一观测** | `journal/` | 16 种事件覆盖 5 大域，一个入口多路输出：trace.jsonl / report.json / snapshot.json |
| **LLM 客户端** | `llm/` | Qwen / DeepSeek / OpenAI 适配，OpenAICompatibleClient 基类统一接口 |
| **工具系统** | `tools/` | ToolHandler/Registry/Executor 三层分离 + 审批机制 + SkillParser |
| **MCP 协议** | `mcp/` | stdio + Streamable HTTP 双传输，McpClient 状态机 + 三个 Handler |
| **Skill 系统** | `skills/` | 扫描注册 + prompt 注入 + SkillsProvider |
| **记忆系统** | `memory/` | L1 Session / L2 日记忆 / L3 蒸馏，MemoryProvider 统一接口 |
| **定时提醒** | `scheduler/` | cron 表达式调度，后台任务执行 |
| **配置管理** | `config/` | YAML → dataclass，类型安全的配置加载 |

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
```

---

## 📊 开发进度

| Phase | 状态 | 说明 |
|:-----:|:----:|------|
| Phase 1 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | ReAct 循环、工具系统、流式输出、审批、调试追踪 |
| Phase 2 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | 多供应商路由、OpenAICompatibleClient 基类、priority 降级、限流器 |
| Phase 3 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | AgentContext、PromptBuilder、AgentResult、message_utils、AgentLogger |
| Phase 4 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | SQLite FTS5 混合检索、LLM 日记忆摘要、Deep Dream 蒸馏、MemoryProvider |
| Phase 5 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | 工具层架构重构：ToolHandler/Registry/Executor 三层分离、去硬编码审批、ToolProvider ABC |
| Phase 6 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | MCP 协议集成：双传输 + McpClient 状态机 + MCPToolProvider + /mcp 命令 |
| Phase 7 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | Skill 系统完善：扫描注册 + prompt 注入 + SkillsProvider + /skills 命令 |
| Phase 8 | ![完成](https://img.shields.io/badge/✅-完成-brightgreen) | Journal 统一观测：16 种事件 / 一个入口多路输出 / trace + report + snapshot |
| Phase 9 | ![搁置](https://img.shields.io/badge/⏸️-搁置-yellow) | Scheduler cron 增强 |
| Phase 10 | ![搁置](https://img.shields.io/badge/⏸️-搁置-yellow) | 取消机制（CancelTokenRegistry） |
| Phase 11 | ![搁置](https://img.shields.io/badge/⏸️-搁置-yellow) | 测试覆盖率 + Web Channel |

---

## 🗺️ 后续开发计划

| 模块         |   状态   | 说明                                       |
|------------|:------:|------------------------------------------|
| 重构数据统计模块 | ✅ 已完成 | 合并 logger/tracer/metrics 为统一 Journal 观测模块 |
| agent实体类包装 | 🔜 待开发 | 将session、memory、content以agent实体包装，拓宽适用范围 |
| 工具安全护栏     | 🔜 待开发 | 控制模型调用工具的边界，完善工具调用的前置处理                  |
| 上下文工程      | 🔜 待开发 | 重构会话上下文的构成，精细化会话时模型可见内容                  |

---

## 📄 License

[MIT](LICENSE)

---

<div align="center">

**🐾 dotClaw · 轻量级开源 AI 个人助手框架**

</div>
