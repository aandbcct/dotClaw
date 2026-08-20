# Harness-Bench Phase 1 结论

本次在相同 `gpt-5.6-luna`、jojocode 接口和 26 项任务下完成了 dotClaw 与 NanoBot 的正式对照记录。两边均有 26/26 项逐题结果、完整代理轨迹和裁判分数；低分任务按原始失败保留，没有删除或刷分。

## 主要观察

- dotClaw 的总体 Completion、Tool Use、Consistency、Robustness、Combined 均高于 NanoBot；完整数值以 `comparison.md` 和两个 summary JSON 为准。
- dotClaw 在长运行状态适应类任务上优势更明显；这与其会话、状态和工具执行链路在本次配置下更完整有关，但仍属于单次 26 题描述性结果，不能外推为统计显著结论。
- NanoBot 的 Workspace/Tool 类任务仍有少量成功，但长运行任务中多次出现模型错误后未恢复、未写出产物的轨迹；这应解释为本次模型/运行配置下的行为，不等同于框架永恒能力上限。
- 两边 Security 均为 100%：本次没有观察到裁判定义的严重安全违规；这不代表所有未测试攻击面都已覆盖。
- dotClaw 平均 token 消耗高于 NanoBot，主要来自较长的工具轨迹和状态任务；成本结论需结合真实计费单价另行估算。

## 限制

- 样本量为每个 Harness 26 项、每题一次最终记录，没有置信区间或重复试验方差。
- jojocode 在运行期间出现过间歇性 502/429/上游过载，已对基础设施无效项重跑或补评分；报告中的失败项仍可能包含模型自身未恢复的错误。
- NanoBot 旧结果曾出现 `unknown-api` 目录归档问题，已在报告生成前按 task_id 归一化；不应把目录名当成模型名。

