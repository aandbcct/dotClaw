# agent_harness_business_v1

PR8 Agent Harness 业务效果数据集。`manifest.json` 是任务族、匹配 Baseline、正式重复次数和 Preflight 规则的唯一真相；`instances/` 中六个 JSON 文件各包含 10 个不同业务实例。

固定口径：

- 6 个任务族、60 个实例；
- 每族 Easy/Medium/Hard 为 2/5/3，总体为 12/30/18；
- Full 与每族唯一匹配 Baseline 对同一 `instance_id + attempt` 配对；
- 正式运行每实例重复 3 次，即 Full 180 条、匹配 Baseline 180 条；
- 每个执行条件一次 Preflight，不进入计分 JSONL；
- 工具、审批、文件和委派副作用仅使用隔离 Fixture 或临时目录。

每个实例声明冻结事实、干扰信息、约束、复杂因素、确定性检查和原子 Judge 判据。加载器还会追加必需判据 `unsupported_claim_absent`，用于单独统计事实越界率。

本目录只定义评测契约，不包含正式模型结果。开发验证、诊断运行和正式采样不可混入同一报告。
