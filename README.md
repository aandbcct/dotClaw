# dotClaw

一个轻量级开源AI个人助手框架，用于学习与实践个人agent助手架构设计，基于ReAct架构，由中间路由层连接客户端与底层组件，实现了多模型适配、多平台适配和会话管理。
每阶段的开发路线、开发日志、代码审查记录都在文档中，方便学习与了解整个框架从0开始的搭建过程。

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
│   ├── mcp/                  # MCP 协议客户端（stdio + Streamable HTTP 双传输）
│   ├── common/              # 通用工具库（限流器 / 单例 / 工具函数）
│   ├── skills/              # Skill 系统（扫描+注册+prompt注入）
│   ├── memory/              # 三级记忆系统（L1 Session / L2 日记忆 / L3 蒸馏）
│   ├── channel/             # 通道（CLI）
│   ├── scheduler/           # 定时提醒
│   ├── config/              # 配置加载（YAML → dataclass）
├── tests/                   # 测试（P1-P7 验收测试全部通过，127/127）
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
| `/tools` | 列出所有可用工具（按来源分组：BUILTIN / MCP） |
| `/mcp` | 查看 MCP servers 连接状态 |
| `/skills` | 列出已加载技能 |
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

# 运行 Phase 4 测试
python tests/test_phase4_acceptance.py

# 运行 Phase 5 测试
python tests/test_phase5_acceptance.py

# 运行 Phase 7 测试
python tests/test_phase7_acceptance.py
```

## 开发进度

| Phase | 状态    | 说明 |
|-------|-------|------|
| Phase 1 | ✅ 完成  | ReAct 循环、工具系统、流式输出、审批、调试追踪 |
| Phase 2 | ✅ 完成  | 多供应商路由、OpenAICompatibleClient 基类、priority 降级、限流器 |
| Phase 3 | ✅ 完成  | AgentContext、PromptBuilder、AgentResult、message_utils、AgentLogger |
| Phase 4 | ✅ 完成  | SQLite FTS5 混合检索、LLM 日记忆摘要、Deep Dream 蒸馏、MemoryProvider |
| Phase 5 | ✅ 完成  | 工具层架构重构：ToolHandler/Registry/Executor 三层分离、builtin/ 子包、去硬编码审批、ToolProvider ABC、日志合并（删除 debug/ 子包） |
| Phase 6 | ✅ 完成  | MCP 协议集成：双传输（stdio+HTTP）+ McpClient 状态机 + 三个 Handler + MCPToolProvider + /mcp 命令 |
| Phase 7 | ✅ 完成  | Skill 系统完善：扫描注册 + prompt 注入 + SkillsProvider + /skills 命令 |
| Phase 8 | 🔜 搁置 | Scheduler cron 增强 |
| Phase 9 | 🔜 搁置 | 取消机制（CancelTokenRegistry） |
| Phase 10 | 🔜 搁置 | 测试覆盖率 + Web Channel |

## 后续开发计划（不分先后）
| 模块       | 状态     | 说明                                               |
|----------|--------|--------------------------------------------------|
| 数据统计模块   | 🔜 进行中 | 记录框架的性能指标，量化后续优化策略的效果                            |
| 多agent协作 | 🔜 待开发 | 记录框架的性能指标，量化后续优化策略的效果                            |

## 已知bug
| 已知bug                                                    | 状态 | 解决方法             |
|----------------------------------------------------------|----|------------------|
| 日记忆会把历史对话的内容也记到每次的对话记忆里，调用太硬性了                           | ×  |                  |
| LOOP时该隐性调用工具的时候没有调用，幻觉大。利于问今天是不是高考，没有调用time_tool，捏造了一个日期 | ×  |                  |
| LOOP时会根据历史对话乱说话                                          | √  | 优化了system prompt |
| 记忆蒸馏时会把assistant的回复当做记忆蒸馏，记忆了不存在的事情                      | ×  |  |


## License

MIT
