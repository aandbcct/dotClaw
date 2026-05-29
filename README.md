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
├── src/dotclaw/          # 源代码
│   ├── agent/            # Agent 核心循环（ReAct）
│   ├── llm/              # LLM 客户端（Qwen / OpenAI 兼容）
│   ├── tools/            # 工具系统（8 个内置工具 + 审批机制）
│   ├── skills/           # Skill 加载器
│   ├── memory/           # 记忆 / 会话持久化
│   ├── channel/          # 通道（CLI）
│   ├── scheduler/        # 定时提醒
│   ├── config/           # 配置加载（YAML → dataclass）
│   └── debug/            # 调试系统（TraceRecord）
├── tests/                # 测试（Phase 1 验收测试全部通过）
├── skills/               # 技能目录
├── data/                 # 运行时数据（sessions / logs）
└── config.yaml           # 配置文件
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
| `/model <名称>` | 切换模型 |
| `/help` | 显示帮助 |
| `/quit` | 退出 |

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
python tests/test_phase1_acceptance.py
```

## 开发进度

| Phase | 状态 | 说明 |
|-------|------|------|
| Phase 1 | ✅ 完成 | ReAct 循环、工具系统、流式输出、审批、调试追踪 |
| Phase 2 | 🔜 待开始 | 上下文窗口管理、长期记忆读写 |
| Phase 3 | 🔜 待开始 | Skill 注入 system prompt |
| Phase 4 | 🔜 待开始 | python / web_search 工具 |
| Phase 5 | 🔜 待开始 | Scheduler cron 增强 |
| Phase 6 | 🔜 待开始 | 测试 + 稳定性 |
| Phase 7 | 🔜 待开始 | 多渠道接入（Web / 企微 / Telegram） |

## License

MIT
