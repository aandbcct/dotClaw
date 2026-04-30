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
│   ├── agent/            # Agent 核心循环
│   ├── llm/              # LLM 客户端
│   ├── tools/            # 工具系统
│   ├── skills/           # Skill 加载器
│   ├── memory/            # 记忆系统
│   ├── channel/          # 通道（CLI/...）
│   ├── scheduler/         # 定时提醒
│   ├── config/            # 配置加载
│   └── debug/             # 调试系统
├── skills/               # 技能目录
├── data/                 # 运行时数据
└── config.yaml           # 配置文件
```

## CLI 命令

- `/new` — 新建对话
- `/list` — 列出所有对话
- `/switch <id>` — 切换对话
- `/delete <id>` — 删除对话
- `/debug` — 查看推理过程
- `/model <name>` — 切换模型
- `/skills` — 列出技能
- `/help` — 帮助
- `/quit` — 退出

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest
```

## License

MIT
