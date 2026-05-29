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
│   ├── agent/               # Agent 核心循环（ReAct）
│   ├── llm/                 # LLM 客户端（Qwen / DeepSeek / OpenAI）
│   ├── tools/               # 工具系统（8 个内置工具 + 审批机制）
│   ├── common/              # 通用工具库（限流器 / 单例 / 工具函数）
│   ├── skills/              # Skill 加载器
│   ├── memory/              # 记忆 / 会话持久化
│   ├── channel/             # 通道（CLI）
│   ├── scheduler/           # 定时提醒
│   ├── config/              # 配置加载（YAML → dataclass）
│   └── debug/               # 调试系统（TraceRecord）
├── tests/                   # 测试（P1 + P2 验收测试全部通过）
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
```

## 开发进度

| Phase | 状态 | 说明 |
|-------|------|------|
| Phase 1 | ✅ 完成 | ReAct 循环、工具系统、流式输出、审批、调试追踪 |
| Phase 2 | ✅ 完成 | 多供应商路由、OpenAICompatibleClient 基类、priority 降级、限流器 |
| Phase 3 | 🔜 待开始 | AgentContext、PromptBuilder、AgentResult、消息工具 |
| Phase 4 | 🔜 待开始 | 三级记忆 + Deep Dream 蒸馏、上下文压缩 |
| Phase 5 | 🔜 待开始 | python / web_search / web_fetch 工具、失败恢复 |
| Phase 6 | 🔜 待开始 | MCP 协议集成 |
| Phase 7 | 🔜 待开始 | Skill 系统完善（注入 / 热加载 / 创建向导） |
| Phase 8 | 🔜 待开始 | Scheduler cron 增强 |
| Phase 9 | 🔜 待开始 | 取消机制（CancelTokenRegistry） |
| Phase 10 | 🔜 待开始 | 测试覆盖率 + Web Channel |

## License

MIT
