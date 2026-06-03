# dotClaw

A lightweight AI Agent framework for learning AI application development.

## 快速开始

```bash
# 安装
pip install -e .

# 配置 API Key
export DOTCLAW_API_KEY=sk-xxxxxxxxxxxxxxxx

# 启动
python -m dotclaw
# 或
dotclaw
```

## 项目结构

```
dotClaw/
├── src/dotclaw/             # 源代码
│   ├── agent/               # Agent 核心循环（ReAct + AgentContext + PromptBuilder）
│   ├── llm/                 # LLM 客户端（Qwen / DeepSeek / OpenAI）
│   ├── tools/               # 工具系统（ToolHandler/Registry/Executor 三层 + 8 个内置工具 + 审批机制）
│   ├── common/              # 通用工具库（限流器 / 单例 / 工具函数）
│   ├── skills/              # Skill 加载器
│   ├── memory/              # 三级记忆系统（L1 Session / L2 日记忆 / L3 蒸馏）
│   ├── channel/             # 通道（CLI）
│   ├── scheduler/           # 定时提醒
│   ├── config/              # 配置加载（YAML → dataclass）
├── tests/                   # 测试（P1-P5 验收测试全部通过，67/67）
├── skills/                  # 技能目录
├── data/                    # 运行时数据（sessions / logs）
├── config.yaml              # 配置文件
└── model_router_config.yaml # 模型路由配置
```

## CLI 命令

| 命令 | 说明 |
|------|------|
| `/new [标题]` | 新建对话 |
| `/list` | 列出所有对话 |
| `/switch <id>` | 切换到指定对话 |
| `/delete <id>` | 删除对话 |
| `/debug` | 查看最近一次推理过程 |
| `/tools` | 列出所有可用工具 |
| `/model <名称>` | 切换模型（支持跨供应商） |
| `/help` | 显示帮助 |
| `/quit` | 退出 |

## 多供应商路由

通过 `model_router_config.yaml` 配置多供应商路由，支持按优先级（priority）自动选择模型和跨供应商降级：

- **Qwen**：`qwen3.7-max`、`qwen-turbo`
- **DeepSeek**：`deepseek-v3`、`deepseek-v4-flash`
- **OpenAI**：`gpt-4o-mini`

路由规则：`purposes.chat.priority` 中 priority 数值最小的 active 模型优先使用；失败时按优先级顺序依次降级。

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行 Phase 1 测试
python tests/test_phase1_acceptance.py

# 运行 Phase 2 测试
python tests/test_phase2_acceptance.py

# 运行 Phase 3 测试
python tests/test_phase3_acceptance.py

# 运行 Phase 5 测试
python tests/test_phase5_acceptance.py
```

## 开发进度

| Phase | 状态 | 说明 |
|-------|------|------|
| Phase 1 | ✅ 完成 | ReAct 循环、工具系统、流式输出、审批、调试追踪 |
| Phase 2 | ✅ 完成 | 多供应商路由、OpenAICompatibleClient 基类、priority 降级、限流器 |
| Phase 3 | ✅ 完成 | AgentContext、PromptBuilder、AgentResult、message_utils、AgentLogger |
| Phase 4 | ✅ 完成 | SQLite FTS5 混合检索、LLM 日记忆摘要、Deep Dream 蒸馏、MemoryProvider |
| Phase 5 | ✅ 完成 | 工具层架构重构：ToolHandler/Registry/Executor 三层分离、builtin/ 子包、去硬编码审批、ToolProvider ABC、日志合并（删除 debug/ 子包） |
| Phase 6 | 🔜 待开始 | MCP 协议集成（基于 ToolProvider ABC + ToolHandler ABC） |
| Phase 7 | 🔜 待开始 | Skill 系统完善（注入 / 热加载 / 创建向导） |
| Phase 8 | 🔜 待开始 | Scheduler cron 增强 |
| Phase 9 | 🔜 待开始 | 取消机制（CancelTokenRegistry） |
| Phase 10 | 🔜 待开始 | 测试覆盖率 + Web Channel |

## License

MIT
