# Benchmark Baselines

存放基线 snapshot 文件，用于 `--baseline` 对比。

## 建立基线
```bash
python -m benchmarks.runner --output benchmarks/reports/baseline_$(date +%Y%m%d)
# 将 reports/snapshots/ 中的文件复制到此目录
```

## 使用基线
```bash
python -m benchmarks.runner --baseline benchmarks/baselines/v1.0.json
```
