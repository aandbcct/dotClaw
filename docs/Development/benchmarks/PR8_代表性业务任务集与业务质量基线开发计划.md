# dotClaw PR8：Agent Harness 业务效果 Benchmark 开发计划

> 状态：已确认开发基线。总体架构见 `PR8_Agent_Harness业务效果Benchmark总体设计.md`。本计划只改造 PR8-B；PR8-A 工程证据收口保持不变。

## 1. 唯一目标

构建覆盖 6 类业务场景、60 个不同实例和匹配 Baseline 的版本化评测模块，使 PR8 能以可追溯正式样本回答 Task Success、Delivery Quality 和 Harness Value，而不是重复执行 10 个简单 Case。

## 2. 迁移边界

### 2.1 包含

1. 建立 Manifest 驱动的新 Dataset 契约及 60 个实例。
2. 扩展单样本字段，表达任务族、难度、执行条件、配对键和质量维度。
3. 将 Judge Spec 从宽泛字符串映射迁移为原子判据列表。
4. 增加 Full/匹配 Baseline 执行矩阵和正式资格校验。
5. 重做业务效果统计、配对提升和最终报告。
6. 保留旧 `runtime_core_v2` 的加载和开发测试兼容。

### 2.2 不包含

- 生产 Runtime 修改；
- PR3 至 PR7 正式实验重跑；
- 正式真实 LLM 采样；
- 真实外部工具和副作用；
- 旧 Dataset 或历史工件删除。

## 3. 新旧映射

| 旧概念 | 新概念 | 迁移策略 | 删除条件 |
|---|---|---|---|
| Python 中固定 8+2/6 个任务 | Manifest 的实例与执行矩阵 | 新路径只读取 Manifest | 本次不删除旧兼容路径 |
| `task_category` | `task_family` | 旧字段只读，新样本写新字段 | 历史快照无需迁移 |
| `execution_mode=fixture/ext` | 候选来源与 `execution_condition` 分离 | 保留 `execution_mode=ext`，新增条件字段 | 本次不删除旧字段 |
| 宽泛 Judge criteria 映射 | 原子判据列表 | 新 Dataset 使用 v3 规范；旧 v2 解析兼容 | 旧 Dataset 不再被新报告消费 |
| 300/180 固定分母 | Manifest 派生 180/180 配对样本 | 新报告拒绝硬编码旧分母 | 旧报告入口仍可读取旧快照 |

## 4. 数据与接口变化

### 4.1 Dataset Manifest

Manifest 必须校验：版本、6 个唯一任务族、每族 10 个实例、2/5/3 难度分布、主要 Baseline、`formal_repeat=3`、`preflight_per_condition=1`、实例与 Judge Spec 引用完整性。

### 4.2 单样本字段

新增可选字段：

- `task_family`
- `instance_id`
- `difficulty`
- `execution_condition`
- `baseline_for`
- `capability_tags`
- `task_success`
- `judge_criterion_dimensions`

字段保持可选，确保 PR1 至 PR7 和 `runtime_core_v2` 历史 JSONL 可以反序列化。新业务正式报告要求这些字段非空。

### 4.3 Judge Spec

新规范的 `criteria` 是对象列表，每项固定包含 `id`、`description`、`dimension`、`required`。允许事实不再限制为最多 8 条；实例仍必须至少提供 3 条允许事实。Prompt 只暴露冻结任务、约束、事实、原子判据和候选交付。

### 4.4 执行矩阵

新业务入口按 Manifest 运行：

```text
Full Harness：60 × 3 = 180 正式样本
Matched Baseline：60 × 3 = 180 正式样本
preflight：每执行条件 1 次，单独标记且不进入正式 JSONL 统计
```

开发期允许选择单个实例或任务族诊断，但正式标记要求完整覆盖全部配对键。

## 5. 实施阶段

### 阶段一：契约与数据骨架

- 新增 Manifest、任务实例和原子 Judge Spec 的加载与严格校验。
- 扩展样本序列化/反序列化字段。
- 先建立 6 个代表性实例的纵向开发切片，再扩展至 60 个。

完成门槛：损坏 Manifest、分布错误、Hard 复杂因素不足、重复实例、缺失 Judge Spec 均明确失败；历史样本继续可读。

### 阶段二：执行条件

- 将候选模型来源与 Harness 执行条件分离。
- 为六个任务族实现匹配 Baseline 的外围装配。
- 固定 `instance_id + attempt` 配对键及一次 preflight 资格。

完成门槛：开发替身能对同一实例运行 Full 和匹配 Baseline；工具及副作用仍被隔离；正式参数不是 3 次或覆盖不完整时拒绝资格。

### 阶段三：Judge 与 Task Success

- 支持原子判据和质量维度。
- 仅在确定性断言通过后调用一次 Judge。
- 统一派生 `task_success`，Judge 错误与质量失败保持不同归因。

完成门槛：宽泛判据、未知维度、缺失判据和非法返回均被拒绝；开放式质量不反向篡改确定性事实。

### 阶段四：统计与报告

- 按执行条件、任务族、难度和质量维度聚合。
- 只对完整配对样本计算 percentage-points 提升。
- 以实例为聚类单位计算 95% 区间。
- 输出约束违反率、Groundedness、事实越界率和调用/时延/归因指标。

完成门槛：缺失配对、不同 Dataset/提交/模型条件混入或 preflight 混入时拒绝正式报告。

### 阶段五：数据扩展、回归与文档

- 将每族扩展至 10 个真实不同实例，完成 12/30/18 分布。
- 运行 Dataset 加载、Judge、统计、报告和旧 v2 兼容定向回归。
- 运行 `compileall` 与 `git diff --check`，增量更新 Benchmark README。

完成门槛：开发级验证全部通过；不生成 X/Y/Z 业务数字，不启动正式采样。

## 6. 测试计划

正常路径：完整 Dataset 加载、60 个实例唯一、Full/Baseline 配对、原子 Judge 通过、任务成功派生、分族/难度/条件报告。

边界路径：实例选择仅允许诊断；正式模式必须覆盖完整矩阵；确定性失败跳过 Judge；取消和超时保持失败。

损坏路径：Manifest 数量或分布错误、Hard 复杂因素不足、重复 ID、Judge 引用缺失、非法质量维度、配对缺失、配置或模型混入均明确失败。

历史兼容：PR1 至 PR7 和 `runtime_core_v2` 缺少新字段时仍可读取；旧正式报告仍按旧入口校验，但不能进入新业务效果报告。

## 7. 推荐提交顺序

1. 总体设计、开发计划和 Manifest 契约；
2. 样本字段与原子 Judge 协议；
3. 六任务纵向执行切片与配对统计；
4. 扩展至 60 个实例；
5. 报告、兼容回归和 README。

## 8. 最终验收

1. 新 Dataset 精确包含 6 个任务族、60 个不同实例和 12/30/18 难度分布；
2. 每个 Hard 实例至少包含两类复杂因素；
3. 每实例具有确定性断言与带维度的原子 Judge Spec；
4. Full 与唯一匹配 Baseline 的正式配对矩阵由 Manifest 派生；
5. 正式参数固定为每实例 3 次，preflight 不进入正式统计；
6. 报告能够计算 Task Success、Delivery Quality、配对 Harness Value、P50/P95、调用次数和失败归因；
7. `runtime_core_v2` 和 PR1 至 PR7 历史样本兼容；
8. 开发验证通过且没有启动正式真实 LLM 实验。
