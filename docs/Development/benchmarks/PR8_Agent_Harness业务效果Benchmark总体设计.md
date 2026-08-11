# dotClaw PR8：Agent Harness 业务效果 Benchmark 总体设计

> 状态：已确认设计基线。固定为 6 个任务族、60 个不同实例、Easy/Medium/Hard 为 12/30/18、每实例正式重复 3 次、每个任务族一个主要匹配基线；正式采样不属于开发阶段。

## 1. 背景与目标

PR3 至 PR7 分别证明并发、恢复、安全、上下文和委派机制在受控条件下可靠。PR8-B 的唯一目标是证明这些机制组合为 Agent Harness 后，用户任务端到端完成得更好。

现有 `runtime_core_v2` 只有 8 个标准 Case 和 2 个 Session 工作流，并把正式口径锁定为 Fixture 300 条、EXT 180 条。它适合作为已完成基础设施的兼容样例，不足以支持“任务族 × 多实例”的业务效果结论。

目标数据流：

```text
Git 跟踪的 Dataset Manifest + 60 个任务实例 + 60 个原子 Judge Spec
    → Full Harness / 每任务族匹配 Baseline
    → 确定性断言
    → 合格候选的一次性 LLM-as-a-Judge
    → BenchmarkSample JSONL + Snapshot
    → Task Success / Delivery Quality / Harness Value 报告
```

## 2. 范围边界

包含：

1. 新建 `agent_harness_business_v1`，固定 6×10 个不同任务实例。
2. 在 Benchmark 外围装配 Full Harness 与按任务族匹配的简化执行条件。
3. 复用现有 Eval、Runtime、Fixture、Judge、失败归因、P50/P95、JSONL 和快照机制。
4. 将开放式质量标准拆成带维度的原子判据。
5. 生成按执行条件、任务族、难度和质量维度分区的报告。

不包含：

- 不修改生产 Runtime 的状态机、持久化或安全语义。
- 不重跑或替代 PR3 至 PR7 的可靠性实验。
- 不执行真实工具、文件、审批或远程委派副作用。
- 不在开发期启动正式真实 LLM 采样。
- 不覆盖 `runtime_core_v1` 或 `runtime_core_v2` 的历史 Dataset 与工件。
- 不做模型横向排名、线上成功率外推或人工评审一致率研究。

## 3. 唯一真相与兼容关系

`agent_harness_business_v1/manifest.json` 是新业务实验口径的唯一真相，声明 Dataset 版本、任务族、实例清单、难度分布、主要 Baseline、正式重复次数和 preflight 规则。执行器和报告不得再以 Python 常量重复声明 60、12/30/18 或每族实例数。

`runtime_core_v2` 保持可读、可运行，其旧 CLI 和旧报告资格继续服务已有开发工件；新调用不得把旧 10 个任务写入新业务报告。待新路径的加载、执行、报告和兼容测试完整后，旧路径才可标记 deprecated，但本次不物理删除。

## 4. Dataset 结构

```text
benchmarks/datasets/agent_harness_business_v1/
├── manifest.json
├── instances/
│   ├── evidence_research.json
│   ├── workspace_engineering.json
│   ├── tool_approval_workflow.json
│   ├── long_context_continuity.json
│   ├── multi_agent.json
│   └── mixed_complex.json
└── README.md
```

每个任务族文件包含 10 个实例；每个实例持有稳定 ID、任务族、难度、复杂因素、用户目标、固定事实、干扰信息、约束、确定性断言、原子 Judge Spec 和能力标签。执行条件由 Manifest 按任务族唯一映射，具体隔离 Fixture 由执行器装配。Hard 实例至少声明两个不同复杂因素；实例之间不得只改名称或数字后复制同一语义。

难度固定为 Easy 12、Medium 30、Hard 18；每个任务族包含 2/5/3 个实例。

## 5. 执行条件与公平性

每个任务族只设置一个主要匹配 Baseline：

| 任务族 | 主要 Baseline | 被移除的 Harness 能力 |
|---|---|---|
| Evidence / Research | `single_pass_research` | 多步资料检索与证据整合编排 |
| Workspace Engineering | `single_agent_workspace` | 持久化工程工作流与验证编排 |
| Tool / Approval Workflow | `direct_tool_without_resume` | 审批暂停、恢复和最终反馈编排 |
| Long-context Continuity | `recent_window_only` | 长期 Context 与历史压缩 |
| Multi-Agent | `single_agent_no_delegation` | Delegation |
| Mixed Complex | `primary_capability_removed` | 实例声明的主要组合能力 |

Full 与匹配 Baseline 必须使用相同实例、候选模型、Judge 模型、Provider 默认 temperature 条件、工具数据、最大迭代预算和 attempt。当前 Provider 传输端不支持请求级 temperature 透传，因此正式 CLI 不接受该参数，并在样本中明确记录 `null`，不得写入一个未实际下发的数值。报告只比较同一 `instance_id + attempt` 的配对样本。

正式真实 LLM 运行不再逐任务预热 5 次。每个执行条件允许一次不进入证据的 preflight；每个实例随后正式重复 3 次。Full 和六类主要 Baseline 各产生 180 条正式样本。

## 6. 结果语义

Task Success 定义为：

```text
deterministic_passed == true
AND 所有 required Judge criterion == pass
```

确定性断言负责 Runtime 终态、工具选择与参数、审批、文件变化、验证命令、委派及可追溯副作用。Judge 只评价最终交付文本，不重新判断运行事实。

每条 Judge 判据包含稳定 ID、原子描述、质量维度和是否必需。质量维度固定为：

- `groundedness`
- `constraint_compliance`
- `completeness`
- `actionability`

Dataset 加载器为每个实例追加统一必需判据 `unsupported_claim_absent`，专门判断候选是否引入允许事实之外的事实性断言；事实越界率只由该判据计算，不把一般事实遗漏误算为越界。

Judge 返回仍为严格 JSON。未知判据、缺字段、额外文本或非法 verdict 继续归因 `judge_error`。

## 7. 报告与证据

报告至少输出：

1. Full 与匹配 Baseline 的端到端 Task Success Rate；
2. 配对成功率提升，单位 percentage points；
3. 以实例为聚类单位的 95% 区间；
4. 关键约束遵守率、Groundedness 和事实越界率；
5. 各任务族、难度、执行条件的成功率、P50/P95、LLM/Tool 调用次数和失败归因；
6. Dataset、提交、模型、配置、Fixture、Judge Prompt 和 JSONL 的追溯信息。

PR8 报告不得把 PR3 至 PR7 的专项样本加入业务分母。PR1 至 PR7 工程证据报告与 PR8-B 业务效果报告继续独立生成。

## 8. 一致性与安全边界

- JSONL 是单次运行派生事实；Snapshot 和 Markdown 是可重算汇总，不是第二份事实源。
- 正式报告只接受同一 Dataset 哈希、提交、模型条件、Judge 版本和完整配对键的样本。
- preflight、诊断运行和正式样本必须有不同资格标记，前两者不得进入正式统计。
- 工具、审批、文件和委派仍由 Fixture 或临时目录隔离；Baseline 不得借“简化”绕过真实外部安全边界。
- 取消、超时和 Judge 错误保留为失败归因，不自动重试成成功样本。

## 9. 风险与限制

- 60 个任务实例仍是受控代表性任务，不等同线上用户分布。
- 同一 Judge 可能存在系统性偏差；原子判据和固定允许事实只能提高可审计性，不能替代人工产品验收。
- 不同任务族采用不同匹配 Baseline，因此总体提升应报告为配对宏平均和明确分族结果，不得伪装成单一历史版本提升。
- 每实例 3 次主要用于观察稳定性；置信区间必须按实例聚类，不能把 180 次运行当作 180 个独立任务。
