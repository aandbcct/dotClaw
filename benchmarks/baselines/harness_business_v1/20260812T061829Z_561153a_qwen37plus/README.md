# PR8 Agent Harness 业务效果正式基线

本目录冻结 PR8 在固定模型条件下完成的正式业务评测证据，基线标识为
`pr8-qwen37plus-workflow-v2`。

## 实验条件

- 源 Commit：`561153a`
- Dataset：`agent_harness_business_v1`，版本 `1`
- 工作流版本：`2`
- Candidate：`qwen3.7-plus`
- Judge：`qwen3.7-plus`
- 任务实例：60
- 每实例重复：3
- Full 样本：180
- matched-baseline 样本：180
- 正式采样：是

## 核心结果

- Full 端到端任务成功率：`104/180`，即 `57.78%`
- matched-baseline 成功率：`28/180`，即 `15.56%`
- Harness 提升：`+42.22` 个百分点
- 聚类 Bootstrap 95% 区间：`+31.67` 至 `+53.33` 个百分点
- Full 关键约束违反率：`5.10%`
- Full 事实越界率：`26.11%`

## 证据边界

`evidence-manifest.json` 是本目录的统一索引，记录实验条件、核心指标以及每个
冻结产物的字节数和 SHA256。两份 JSONL 是正式样本事实来源，两份 Snapshot
保存对应条件的聚合结果，业务报告用于阅读和复核。

本目录中的“matched-baseline”指同批任务的简化 Harness 对照条件；整个目录则是
供后续测试比较的 PR8 v1 历史参考基线，两者含义不同。

运行控制目录中的 PID、stdout、stderr 和启动日志不属于业务结果证据，因此未纳入
版本管理。后续若修改 Dataset、Judge Spec、成功判定、工作流或固定模型条件，应建立
新的基线目录，不覆盖本目录。
