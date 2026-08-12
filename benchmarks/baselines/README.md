# Benchmark Baselines

存放经过资格校验、去敏和哈希冻结的正式基线证据，用于后续版本对比。
`benchmarks/reports/` 仅作为运行工作区并保持 Git 忽略；正式证据应从报告目录复制到
对应版本的基线目录，不直接放行临时报告。

当前基线类型：

- `runtime_core_v1/`：Runtime 核心性能基线。
- `reliability_*_v1/`：恢复、安全、上下文和委派等可靠性基线。
- `harness_business_v1/`：PR8 Agent Harness 业务效果正式基线。

## 建立基线
```bash
python -m benchmarks.runner --output benchmarks/reports/baseline_$(date +%Y%m%d)
# 校验并去敏后，将快照、样本和证据清单复制到对应版本目录
```

## 使用基线
```bash
python -m benchmarks.runner --baseline benchmarks/baselines/v1.0.json
```
