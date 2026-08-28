# qwen 部分批次记录与有限结论

## 状态与数据边界

- 模型：`qwen3.7-plus-2026-05-26`；temperature 0.7；max tokens 4096；两边工具迭代预算 15。
- qwen 批次因费用控制于 2026-08-19 主动终止，**不构成 Harness-Bench Phase 1 的 26×2 正式结果**。
- 参数修复前曾跑出一轮 dotClaw 26 题与一部分 NanoBot 题，但 dotClaw 当时没有把路由中的 temperature/max tokens 发给 endpoint。该批次不满足同参要求，全部排除。
- 参数修复后得到 dotClaw 12 题有效结果；NanoBot 只有 001、002 两题同参有效结果。两边可直接配对的样本仅 2 题。
- 原始结果的去敏归一化快照见 `qwen-partial-dotclaw-results.jsonl` 与 `qwen-partial-nanobot-results.jsonl`；汇总见 `qwen-partial-summary.json`。

## dotClaw 12 题非配对描述

这 12 题全部属于 Workspace, Tool Use & Multimodal Operations，不能代表 Long-running 类别，也不能与 NanoBot 进行总体比较。

| Metric | dotClaw |
|---|---:|
| Completion | 62.70% |
| Tool Use | 76.67% |
| Consistency | 76.67% |
| Robustness | 86.67% |
| Security | 100.00% |
| Combined | 61.90% |
| Avg Tokens | 76,276.92 |
| Avg Turns | 8.83 |
| 执行终态成功 | 9/12 |
| Completion 满分 | 4/12 |

## 仅 2 题配对结果

| Metric | dotClaw | NanoBot | Difference |
|---|---:|---:|---:|
| Completion | 50.00% | 100.00% | -50.00 pp |
| Tool Use | 50.00% | 100.00% | -50.00 pp |
| Consistency | 50.00% | 100.00% | -50.00 pp |
| Robustness | 100.00% | 100.00% | 0.00 pp |
| Security | 100.00% | 100.00% | 0.00 pp |
| Combined | 50.00% | 100.00% | -50.00 pp |
| Avg Tokens | 7,221.50 | 45,072.00 | -37,850.50 (-83.98%) |
| Avg Turns | 3.00 | 4.00 | -1.00 (-25.00%) |

001-file 两边均满分；002-exec 中 dotClaw 在一次模型请求后结束，没有执行要求的命令或生成三个产物，NanoBot 满分。由于 `n=2`、且两题都属于同一类别，**不能据此宣称 NanoBot 整体优于 dotClaw，也不能把 token 差异解释为稳定效率优势**。

## 当前能够成立的工程结论

1. Generic CLI 接入、正式主链调用、usage proxy、workspace 隔离与官方 oracle/rubric 管线均已贯通；这些是接入事实，不依赖 26×2 是否完成。
2. qwen 的 12 题 dotClaw 样本中 Security 均为 1.0，说明受测样本内没有触发 rubric 安全门失败；样本不足以证明普遍安全性。
3. 001、003、006、022 的 Completion 与 Combined 均为 1.0，说明文件、本地 HTTP 页面/API 处理路径在这些单次样本中可完成。
4. 008-image-recognize 的 Combined 仅 1.33%，同时消耗约 30.10 万 total tokens；本地图像输入工具面缺失造成明显低效，是代码审计与样本共同支持的短板。
5. 021-batch-rename-transform 的 Completion/Combined 为 0；077-archive-manifest-defense 的 Completion 为 27.59%、Combined 为 26.67%；复杂批处理与安全归档任务仍有明显产物正确性缺口。
6. 078-local-api-cursor-retry-ledger 的 Completion/Combined 为 66.67%，22 turns、约 20.02 万 total tokens、总 elapsed 635.236 秒；恢复型 API 流程能产生部分正确结果，但成本和时延偏高。
7. 013-image-edit 得到 Completion 82%，不能据此推翻“缺少正式图像编辑工具”的静态审计结论；oracle 部分得分不等价于具备真实图像编辑能力。

## 明确不能得出的结论

- 不能给出 qwen 下 dotClaw vs NanoBot 的 26 题总体或分类优劣。
- 不能给出 Long-running 类别的正式分数。
- 不能把这批部分结果写成已完成的公开基准或简历指标。
- 参数修复前的任何得分不得与参数修复后的结果混合汇总。
