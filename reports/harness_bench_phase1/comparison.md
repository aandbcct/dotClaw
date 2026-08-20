# Harness-Bench Phase 1 Comparison

## Overall

| Metric | dotClaw | NanoBot | Difference |
|---|---:|---:|---:|
| Completion | 40.18% | 15.50% | +24.68 pp |
| Tool Use | 52.46% | 14.31% | +38.15 pp |
| Consistency | 58.19% | 11.04% | +47.15 pp |
| Robustness | 52.88% | 33.00% | +19.88 pp |
| Security | 100.00% | 100.00% | +0.00 pp |
| Combined | 33.04% | 8.62% | +24.42 pp |
| Avg Tokens | 20645.04 | 11060.00 | +9585.04 (+86.66%) |
| Avg Turns | 5.19 | 5.73 | -0.54 (-9.40%) |

## Workspace, Tool Use & Multimodal Operations

| Metric | dotClaw | NanoBot | Difference |
|---|---:|---:|---:|
| Completion | 28.23% | 19.46% | +8.77 pp |
| Tool Use | 37.47% | 24.80% | +12.67 pp |
| Consistency | 45.33% | 19.13% | +26.20 pp |
| Robustness | 53.00% | 57.20% | -4.20 pp |
| Security | 100.00% | 100.00% | +0.00 pp |
| Combined | 24.49% | 14.94% | +9.55 pp |
| Avg Tokens | 13827.87 | 19170.67 | -5342.80 (-27.87%) |
| Avg Turns | 3.80 | 4.87 | -1.07 (-21.92%) |

## Long-running Autonomy & State Adaptation

| Metric | dotClaw | NanoBot | Difference |
|---|---:|---:|---:|
| Completion | 56.48% | 10.10% | +46.38 pp |
| Tool Use | 72.91% | 0.00% | +72.91 pp |
| Consistency | 75.73% | 0.00% | +75.73 pp |
| Robustness | 52.73% | 0.00% | +52.73 pp |
| Security | 100.00% | 100.00% | +0.00 pp |
| Combined | 44.69% | 0.00% | +44.69 pp |
| Avg Tokens | 29941.18 | 0.00 | +29941.18 (relative N/A) |
| Avg Turns | 7.09 | 6.91 | +0.18 (+2.63%) |

## 口径

- Completion、Tool Use、Consistency、Robustness、Security、Combined 的差值使用百分点（pp）。
- Turns 使用官方 usage proxy 的 request_count；Tokens 使用同一 proxy 的 total_tokens。
- 本表是每个 task 单次正式运行的描述性结果，不提供置信区间，也不把随机差异解释为稳定提升。
