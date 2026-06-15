# dotClaw Benchmark 评测系统

## 是什么

一套自动化框架性能评测脚本，测量 dotClaw 各核心模块的耗时、吞吐量、稳定性。

与 `tests/`（功能正确性）互补——tests 回答"对不对"，benchmarks 回答"快不快"。

## 目录结构

```
benchmarks/
├── runner.py          # 评测入口（CLI）
├── cases/             # 6 个评测用例
│   ├── init_perf.py       # 初始化性能
│   ├── tool_dispatch.py   # 工具调度延迟
│   ├── llm_stream.py      # LLM 流式延迟
│   ├── memory_perf.py     # 记忆检索性能
│   ├── skill_load.py      # Skill 加载性能
│   └── stress.py          # 压力测试
├── dataset/           # 测试数据集（自动生成）
│   ├── sample_skills/         # 100 个测试 Skill
│   ├── memory_corpus/         # 100 / 1000 / 10000 行语料
│   └── stress_prompts.json    # 压力测试用 prompts
├── reports/           # 报告输出（gitignore）
│   ├── benchmark_report_*.md
│   └── snapshots/
└── baselines/         # 基线快照（git tracked，用于回归对比）
```

## 快速开始

### 1. 生成测试数据（只需一次）

```bash
python scripts/generate_benchmark_dataset.py
```

### 2. 跑一次评测

```bash
# 跑全部 case（默认 warmup=3, repeat=10）
python -m benchmarks.runner

# 只跑指定的 case
python -m benchmarks.runner --filter init_perf,tool_dispatch

# 调节参数
python -m benchmarks.runner --warmup 5 --repeat 30
```

### 3. 看结果

- **控制台**：直接输出各 case 的 P50/P95/Min/Max
- **Markdown 报告**：`benchmarks/reports/benchmark_report_*.md`
- **快照文件**：`benchmarks/reports/snapshots/*.json`

## 建立基线 & 回归对比

### 建立基线

跑一轮"干净"的评测（确保没有其他程序抢资源），然后手动保存为基线：

```bash
# 跑一轮
python -m benchmarks.runner --warmup 5 --repeat 30

# 把刚跑的 snapshot 复制到 baselines/
# 报告里 Summary 表的数据就是你的基线值
```

然后把 `benchmarks/baselines/` 目录 commit 到 git，作为团队的基准参考。

### 回归对比

改完代码后，跟基线对比看有没有退化：

```bash
python -m benchmarks.runner --baseline benchmarks/baselines/<baseline_file>.json
```

输出会标注哪些指标改善了、哪些退化了。

## 6 个 Case 说明

| Case | 测什么 | 需要 LLM? | 核心指标 |
|------|--------|-----------|---------|
| `init_perf` | Config/LLM/Skill/Tool/Memory 等模块初始化耗时 | 不需要 | Agent Init P95 |
| `tool_dispatch` | ToolExecutor 从收到调用到执行 handler 的调度开销 | 不需要 | Dispatch P95 |
| `llm_stream` | LLM 流式 API 的 TTFT / TPS / E2E | 真实 API（默认 qwen3.7-max） | TTFT [EXT] |
| `memory_perf` | SQLite FTS5 在不同数据量下的检索延迟 | 不需要 | P95 Retrieval |
| `skill_load` | SkillScanner 扫描不同数量 Skill 的耗时 | 不需要 | Scan P50 |
| `stress` | 并发工具调用 / 大上下文 / 超长 ReAct 循环 | 不需要 | E2E P50 |

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--filter` | 全部 | 指定要跑的 case，逗号分隔。如 `--filter init_perf,stress` |
| `--warmup` | 3 | 前 N 次迭代丢弃（消除冷启动偏差） |
| `--repeat` | 10 | 实际测量迭代次数。CI 建议 >= 30 |
| `--baseline` | 无 | 基线 snapshot 文件路径，用于回归对比 |
| `--output` | `benchmarks/reports` | 报告输出目录 |

> `llm_stream` 默认调用 `config.yaml` 中 `llm.default_model` 配置的模型（当前为 qwen3.7-max）。
> 如需切换模型，修改 `config.yaml` 中的 `default_model` 即可。

## 注意事项

### 跑 benchmark 时的环境
- **关掉其他重负载程序**（浏览器、IDE 索引、杀毒扫描），否则数据波动大
- **Windows 电源模式设为"高性能"**，避免 CPU 降频
- **同一台机器上对比才有意义**，不同机器的绝对值不可比

### 数据解读
- **P50（中位数）**：典型性能。比平均值更能代表"大多数情况"
- **P95**：最差 5% 的情况。反映稳定性
- **warmup 必须 >= 3**：Python import 缓存、文件系统缓存会影响第 1 次测量

### 限制
- `llm_stream` 默认调用真实 API（qwen3.7-max），**有费用**。不想花钱就 `--filter` 排除
- 当前默认模型 qwen3.7-max 是推理模型，TTFT 偏高（~6s）。建议用 fast 模型（如 deepseek-v4-flash ~0.8s）来测框架流式链路
- Windows 上 `time.time()` 精度 ~15ms，sub-ms 操作可能显示 0（不影响趋势）
- 报告中的 `[EXT]` 标记表示包含外部依赖延迟（网络/API），与框架内部延迟含义不同
