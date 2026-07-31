# dotClaw Trace、Eval 与 Replay 控制面 PRD

## 0. 文档信息

| 项目 | 内容 |
|---|---|
| 文档版本 | v2.1 |
| 文档状态 | 开发边界基准 |
| 适用项目 | dotClaw |
| 核心范围 | Trace、Eval、Playback、Re-execution、Regression Gate、Failure Attribution、OTLP |
| 阶段数量 | 4 个阶段 |
| PR 数量 | 8 个 PR |
| 核心闭环 | Trace → Eval → Replay/Regression |
| 增强能力 | Failure Attribution、OTLP |
| 不包含 | Journal 改造、生产恢复替换、真实副作用回放、LLM Judge、成本金额门禁 |

---

# 1. 背景

dotClaw Runtime v4 已经具备一套分离的运行记录体系：

- `AgentRun` 保存运行身份、状态、终态和统计；
- `RunMessage` 保存用户输入、模型响应、工具调用和工具结果；
- `ContextVersion` 保存模型调用时使用的稳定上下文快照；
- `RunEvent` 保存按 sequence 追加的运行审计事实；
- `RunCheckpoint` 保存审批等待、进程中断等安全边界的最小恢复状态。

这些内容分别存放于：

```text
run.json
messages.json
events.jsonl
checkpoint.json
```

当前 Runtime 的记录体系主要服务于：

- Runtime 审计；
- 状态恢复；
- 持久化一致性；
- 低层故障排查。

目前缺少一个稳定的语义读模型，将分散记录统一转换为：

- 一次 Run 的完整执行链路；
- 可供 Eval 和 Replay 使用的轨迹；
- 可供回归比较和失败归因使用的公共输入；
- 可导出到 JSON 或 OpenTelemetry 的标准结构。

---

# 2. 产品定位

## 2.1 总体定位

本项目建立一个独立于 Runtime 执行平面的分析与评测控制面：

```text
Runtime 权威运行记录
        ↓
RunTrace
        ↓
查询 / 导出 / Eval / Playback / 回归比较 / 失败归因
```

该控制面不改变 Runtime 状态机，也不成为新的生产恢复机制。

## 2.2 Trace 的定位

`RunTrace` 是：

> Runtime 权威运行记录之上的标准化语义读模型。

Trace 负责：

- 关联分散的 Run、Event、Message 和 ContextVersion；
- 将开始和完成事件配对；
- 形成 LLM、Tool、Approval、Delegation 等语义步骤；
- 统一处理历史字段缺失和不完整记录；
- 为上层分析能力提供一致输入。

Trace 不负责：

- 恢复或继续生产 Run；
- 替代 `events.jsonl`；
- 修改 Runtime 事实；
- 决定 Runtime 状态；
- 自动写入 Conversation；
- 成为第二套运行事实源。

## 2.3 为什么上层能力不直接解析 RunEvent

`RunEvent` 只保存：

- 事件顺序；
- 消息引用；
- 摘要；
- 部分结构化数据。

完整工具参数保存在 Assistant `RunMessage.tool_calls` 中，工具结果也保存在相应 `RunMessage` 中。

若 Eval、Replay、OTLP 和 Failure Attribution 各自直接解析 Event，它们都需要重复实现：

- Event 与 Message 关联；
- Event 与 ContextVersion 关联；
- LLM 和 Tool 事件配对；
- 不完整记录处理；
- 历史兼容；
- 轨迹统计。

因此统一通过 Trace 解释 Runtime 记录，避免不同模块对同一次 Run 产生不同理解。

---

# 3. 产品目标

## 3.1 核心目标

1. 根据 `run_id` 读取一次运行的完整权威记录；
2. 动态构建稳定、可版本化的 RunTrace；
3. 支持查询运行中或已终止 Run 的轨迹；
4. 支持显式导出 JSON Trace；
5. 建立确定性 EvalCase 和 Fixture 执行环境；
6. 支持历史运行转化为可审核的评测用例；
7. 支持确定性 Playback 和受控 Re-execution；
8. 在 CI 中阻止确定性行为回退；
9. 对失败进行基于规则的初步归因；
10. 支持将 Trace 导出到 OpenTelemetry。

## 3.2 MVP 范围

前三个阶段形成 MVP：

```text
Trace
→ Deterministic Eval
→ Playback / Re-execution
→ Regression Gate
```

第四阶段为增强能力，不阻塞 MVP 使用。

---

# 4. 非目标

## 4.1 不改造 Journal

Journal 已不在当前 Runtime 主链使用，本项目不处理：

- Journal 工件迁移；
- Journal Event 兼容；
- Journal Snapshot 或 Report；
- Journal 与 RunTrace 的统一；
- Journal 模块删除。

Journal 后续可作为独立技术债归档或移除。

## 4.2 不替代 RunEvent

RunEvent 继续作为 Runtime 的追加式审计记录。

RunTrace 只读取并解释 RunEvent，不反向写入或替代它。

## 4.3 不替代 checkpoint/resume

生产恢复继续使用：

```text
AgentRun
+ RunCheckpoint
+ RunMessage[]
+ Active ContextVersion
```

Replay 不使用生产恢复入口，不修改原 Run。

## 4.4 第一版不引入 LLM Judge

第一版 Scorer 必须是：

- 确定性的；
- 可离线运行的；
- 可重复的；
- 可以进入 CI 的。

## 4.5 不回放真实副作用

Playback 和 Eval 默认禁止：

- 写入真实工作目录；
- 执行真实进程；
- 修改真实 Memory；
- 调用生产 MCP 写操作；
- 发送真实网络请求；
- 复用生产凭证。

## 4.6 不实现生产费用金额门禁

在没有模型定价版本和价格快照前，不实现人民币或美元费用回归判断。

---

# 5. 核心概念

## 5.1 Runtime 权威运行记录

本项目所称的 Runtime 权威运行记录包括：

```text
AgentRun
RunEvent[]
RunMessage[]
ContextVersion[]
```

这些内容描述一次 Run 已经发生的事实。

`RunCheckpoint` 和 `ApprovalRecord` 属于恢复控制记录，不是普通 Trace 构建的必要输入。

## 5.2 RunTrace

RunTrace 至少包括：

```text
schema_version
run metadata
source metadata
spans
messages
context_versions
metrics
issues
```

来源信息至少包含：

```text
source_run_status
source_event_sequence
source_message_sequence
source_context_version_count
record_hash
assembled_at
is_partial
```

其中：

- `record_hash` 基于权威记录生成，不包含 `assembled_at`；
- `is_partial=true` 表示 Run 尚未进入最终完成状态，或记录存在未配对步骤；
- 相同权威记录输入应生成语义等价的 RunTrace。

## 5.3 TraceSpan

Trace V1 只包含以下 Span：

| SpanKind | 含义 |
|---|---|
| `RUN` | 整个 AgentRun |
| `LLM` | 一次模型调用 |
| `TOOL` | 一次工具调用 |
| `APPROVAL` | 一次审批等待和解决过程 |
| `DELEGATION` | 一次子 Agent 委派 |

## 5.4 ContextVersion 的定位

ContextVersion 是模型实际使用的上下文快照，不是普通执行 Span。

关系为：

```text
LLM Span
    └── context_version
            ↓
      ContextVersion
```

Trace V1 不创建 `CONTEXT` Span。

## 5.5 Compaction 的定位

当前 RunEvent 没有稳定的 Compaction Started/Completed 事件。

因此 Trace V1：

- 不创建 `COMPACTION` Span；
- 可以展示已经持久化的压缩版本或标记；
- 不推测压缩开始时间和结束时间；
- 后续若需要评测压缩耗时，再新增 Runtime Event 并升级 Trace Schema。

## 5.6 EvalCase

EvalCase 是可重复执行的评测任务，至少包括：

```text
case_id
schema_version
name
agent_id
input
conversation_fixture
llm_fixture
tool_fixtures
expectations
tags
source_trace
execution_mode
```

## 5.7 Playback

Playback 使用历史或人工定义的固定输出：

```text
ScriptedLLMPort
+ FixtureToolPort
+ 隔离 Runtime 仓储
```

Playback 用于验证：

- Runtime 状态机；
- 工具编排；
- 审批和委派流程；
- Event 生成；
- Trace 组装；
- Policy 行为；
- 确定性回归。

Playback 不验证当前模型是否做出更好的决策。

## 5.8 Re-execution

Re-execution 使用：

```text
当前 LLM / 当前 Prompt
+ FixtureToolPort
+ 隔离 Runtime 仓储
```

Re-execution 用于验证：

- Prompt 调整后的行为变化；
- 模型路由或模型升级后的结果；
- 当前 Agent 是否选择正确工具；
- 当前回答是否满足目标。

Re-execution 具有不确定性，不进入强制 PR Gate。

## 5.9 checkpoint/resume 与 Replay 的边界

```text
checkpoint/resume
= 在原 run_id 上继续未完成生产任务

Playback / Re-execution
= 创建新的隔离 Run，验证历史任务或行为
```

二者可以复用 RuntimeEngine 和 DTO，但没有替代关系。

---

# 6. Trace 的构建与导出时机

## 6.1 Runtime 执行期间

RuntimeEngine 只负责写入权威运行记录：

```text
AgentRun
RunEvent
RunMessage
ContextVersion
Checkpoint
```

RuntimeEngine 不直接生成 RunTrace，也不自动调用 JSON 或 OTLP Exporter。

## 6.2 查询时动态构建

默认查询路径：

```text
TraceService.get_trace(run_id)
→ 加载 Run/Event/Message/ContextVersion
→ 组装 RunTrace
```

查询时：

- 从最新 Runtime 记录重新构建；
- 默认只在内存中返回；
- 不自动写入 `trace.json`；
- 可以查询运行中、审批等待、中断或终态 Run。

## 6.3 非终态 Trace

`RUNNING`、`WAITING_APPROVAL` 和 `INTERRUPTED` 状态允许查询部分 Trace。

部分 Trace 必须标记：

```text
is_partial = true
source_run_status
source_event_sequence
assembled_at
```

部分 Trace 主要用于调试和查看当前进度。

默认不得将部分 Trace 转换为正式 EvalCase 或加入回归数据集。

## 6.4 JSON 导出

JSON Trace 是显式导出工件，不是默认持久化文件。

```text
RunTrace
→ JsonTraceExporter
→ {run_id}.trace.json
```

规则：

- 主要用于终态 Run 的归档、共享和离线分析；
- 默认只允许导出终态 Trace；
- 调试场景可以显式允许导出部分 Trace；
- 导出文件必须保留来源状态和来源 sequence；
- 再次显式导出时允许覆盖旧文件；
- JSON 文件不是后续查询的权威来源。

## 6.5 Eval 构建时机

EvalRunner 在测试 Run 返回预期执行结果后构建 RunTrace。

若 EvalCase 配置审批 Fixture，可自动解决审批并继续原测试 Run。

若期望结果本身是 `WAITING_APPROVAL`，可基于部分 Trace 评分，但该 Case 必须显式声明。

---

# 7. 统计数据归属

统计数据按性质分为三层。

## 7.1 Run 级权威统计

放在 `run.json` 的 `AgentRun.statistics` 中：

```text
llm_call_count
tool_call_count
tokens_in
tokens_out
duration_ms
resume_count
```

这些数据回答：

> 这次 Run 最终消耗了多少基础资源？

它们应在不构建 Trace 的情况下也能快速读取。

## 7.2 过程测量事实

放在 `RunEvent` 或 `RunMessage` 中：

```text
单次 LLM 模型
单次 LLM Token
Tool 开始与完成
Tool 状态
审批结果
Delegation 结果
Provider 路由或重试信息
```

这些数据回答：

> 某一步实际发生了什么？

## 7.3 Trace 派生指标

放在 `RunTrace.metrics` 中：

```text
LLM 总耗时
Tool 总耗时
审批等待时长
最长 Tool 耗时
失败 Tool 数量
未完成 Span 数量
关键路径耗时
```

这些指标可以根据权威记录重新计算，不能成为唯一事实来源。

## 7.4 跨 Run 聚合

以下数据不放在某个 Run 的 `run.json` 或 RunTrace 中：

```text
最近 100 次 Run 成功率
P95 LLM 延迟
Tool 失败率
平均 Token
失败原因占比
版本前后回归比例
```

它们由后续 Metrics Report、EvalResult、RegressionReport 或观测数据库负责。

---

# 8. 总体架构

```text
src/dotclaw/
├── runtime/
│   └── 生产执行、权威记录、Checkpoint 和 Resume
│
├── trace/
│   ├── models.py
│   ├── assembler.py
│   ├── service.py
│   └── exporters/
│       ├── json_exporter.py
│       └── otlp_exporter.py
│
└── eval/
    ├── models.py
    ├── fixtures.py
    ├── runner.py
    ├── dataset.py
    ├── playback.py
    ├── reexecution.py
    ├── compare.py
    ├── gate.py
    ├── attribution.py
    └── scorers/
```

依赖方向：

```text
runtime domain/application ports
        ↑
trace
        ↑
eval
```

约束：

- Runtime 不依赖 Trace；
- Runtime 不依赖 Eval；
- Trace 可以读取 Runtime Domain Facts；
- Eval 可以调用 RuntimeEngine 和 Trace；
- OTLP SDK 只能存在于 Trace Adapter；
- Exporter 失败不得改变 Run 结果。

---

# 9. 阶段和 PR 划分

# 阶段一：Trace 基础闭环

## 阶段目标

根据 `run_id` 动态构建稳定的结构化 RunTrace。

## PR1：运行事件读取契约

### 目标

补齐 Runtime 运行事件的读取闭环，为 PR2 构建 Trace 提供完整输入。

### 包含范围

1. `run_event_from_dict()`；
2. `RunRepository.load_events()`；
3. 文件仓储读取实现；
4. 内存仓储读取实现；
5. sequence 和 run_id 严格校验；
6. 补充审批事件必要结构化字段；
7. 单元测试和 Runtime 回归测试。

### 审批事件补充

`WAITING_APPROVAL.data`：

```text
approval_id
call_id
```

`APPROVAL_RESOLVED.data`：

```text
approval_id
approved
```

### 不包含

- `trace/` 模块；
- RunTrace；
- TraceSpan；
- TraceAssembler；
- TraceService；
- JSON 导出；
- Trace Metrics；
- 统计模型修复；
- Journal 修改。

### 验收标准

- RunEvent 可从 JSON 反序列化；
- 文件与内存仓储均支持 `load_events()`；
- 损坏 JSON、sequence 异常和 run_id 不匹配明确失败；
- 历史缺少新审批字段的 Event 仍可读取；
- Runtime 状态机行为不变；
- 现有恢复和审批测试通过。

## PR2：RunTrace、TraceService 与 JSON 导出

### 目标

加载 Runtime 权威记录，直接组装 RunTrace，并支持动态查询和显式导出。

### 内部链路

```text
run_id
→ TraceService
→ 加载 Run/Event/Message/ContextVersion
→ assemble_trace()
→ RunTrace
```

只保留两个有效职责：

```text
TraceService：加载数据
assemble_trace：解释数据
```

不引入公开的 `RunRecord` 或 `RunRecordReader`。

### 包含范围

1. RunTrace、TraceSpan、TraceIssue、TraceMetrics；
2. LLM、Tool、Approval、Delegation Span 配对；
3. Message 和 ContextVersion 关联；
4. 部分 Trace 标记；
5. Trace Metrics；
6. TraceService；
7. JsonTraceExporter；
8. Golden Trace 测试。

### 不包含

- Eval；
- Playback；
- OTLP；
- 自动终态导出；
- RuntimeEngine 调用 Trace；
- Context Span；
- Compaction Span。

### 验收标准

- 相同权威记录生成语义等价 Trace；
- 所有关键步骤正确配对；
- 部分和不完整 Trace 被明确标记；
- Query 默认不产生文件；
- JSON 导出是显式行为；
- Golden Trace 稳定通过。

# 阶段二：确定性 Eval

## 阶段目标

建立无需真实模型和真实外部服务即可重复执行的评测环境。

## PR3：EvalCase 与 Fixture Environment

### 目标

定义评测用例和隔离执行依赖。

### 包含范围

1. EvalCase；
2. LLMFixture、ToolFixture、ApprovalFixture、DelegationFixture；
3. ScriptedLLMPort；
4. FixtureToolPort；
5. Fixture DelegationPort；
6. 隔离 RunRepository；
7. 隔离 CheckpointRepository；
8. 固定 TokenCounter；
9. 固定 HistoryCompactor；
10. NORMAL 和 STRICT 两种 Fixture 匹配模式。

### 安全要求

- 未匹配 Fixture 的调用直接失败；
- 不回退真实 LLMPort；
- 不回退真实 ToolPort；
- 不访问生产 Session；
- 不写生产目录。

### 不包含

- Scorer；
- Dataset；
- Trace 自动转 Case；
- Regression Gate；
- 真实模型；
- 真实网络。

## PR4：EvalRunner 与 Deterministic Scorers

### 目标

执行 EvalCase 并输出确定性评测结果。

### 执行链路

```text
EvalCase
→ 隔离 RuntimeEngine
→ Eval Run
→ RunTrace
→ Scorers
→ EvalResult
```

### 第一批 Scorer

| Scorer | 作用 |
|---|---|
| `RunStatusScorer` | 运行状态 |
| `ToolSequenceScorer` | 工具调用路径 |
| `ToolArgumentScorer` | 关键参数 |
| `ApprovalScorer` | 审批行为 |
| `PolicyScorer` | 允许或拒绝结果 |
| `OutputAssertionScorer` | 精确、包含、正则断言 |
| `ContextRetentionScorer` | 指定信息是否进入上下文 |
| `TokenBudgetScorer` | Token 上限 |
| `IterationBudgetScorer` | LLM 和循环次数 |

明确：

```text
Runtime completed
≠
Task succeeded
```

任务成功由 Expectations 和 Scorers 决定。

# 阶段三：Replay 与 Regression

## 阶段目标

将历史问题转化为评测数据集，并建立回归验证机制。

## PR5：TraceToEvalCaseDraft 与 Dataset

### 目标

将终态 RunTrace 转换为可审核的 EvalCaseDraft。

### 包含范围

1. 提取用户输入、Agent、Conversation、LLM 输出、Tool 调用、审批、委派；
2. 生成基础 Expectations；
3. 提取 Token 和调用次数基线；
4. 脱敏；
5. 人工确认；
6. Dataset 管理；
7. Case 来源追溯。

### 默认规则

```text
EvalCaseDraft
→ 显式确认
→ EvalCase
→ Dataset
```

默认只接受终态 Trace。

部分 Trace 不得自动加入 Dataset。

## PR6：Playback、Re-execution 与 RegressionGate

### 目标

对 Dataset 执行回放或重新执行，并输出回归结论。

### 执行模式

```python
class EvalExecutionMode(StrEnum):
    PLAYBACK = "playback"
    REEXECUTE = "reexecute"
```

### Playback

- 使用 ScriptedLLMPort；
- 使用 FixtureToolPort；
- 完全离线；
- 确定性；
- 允许进入 PR Gate。

### Re-execution

- 使用当前 LLMPort；
- 使用 FixtureToolPort；
- 创建新的隔离 Run；
- 不复用生产 Checkpoint；
- 不修改原 Session 和原 Run；
- 用于手动或 Nightly Eval；
- 不阻塞普通 PR。

### RegressionGate

PR Gate 只允许：

- Playback；
- Fixture Tool；
- 固定 TokenCounter；
- 确定性 Policy；
- 确定性 Context；
- 确定性 Scorer。

# 阶段四：高级分析与外部观测

## 阶段目标

增加自动诊断和标准协议导出，不改变前三阶段核心闭环。

## PR7：FailureAttributor

### 目标

基于 RunTrace 和 EvalResult 进行规则型失败归因。

### 第一版类别

```text
CONTEXT_BUILD_FAILURE
CONTEXT_BUDGET_EXCEEDED
CONTEXT_INFORMATION_LOST
LLM_UNAVAILABLE
LLM_INVALID_ACTION
WRONG_TOOL_SELECTED
TOOL_ARGUMENT_INVALID
TOOL_EXECUTION_FAILED
POLICY_DENIED
UNNECESSARY_APPROVAL
APPROVAL_REJECTED
DELEGATION_FAILED
GOAL_NOT_COMPLETED
ITERATION_BUDGET_EXCEEDED
TOKEN_REGRESSION
UNKNOWN
```

核心原则：

> 定位最早出现且足以决定最终失败的步骤。

结果包括：

```text
category
confidence
decisive_span_id
evidence
secondary_causes
```

约束：

- 只读取 RunTrace 和 EvalResult；
- 不修改 Runtime 事实；
- 不将归因写回 Event；
- 证据不足时返回 `UNKNOWN`；
- LLM 后续只能用于解释，不决定 CI 真值。

## PR8：OTLP Exporter

### 目标

将稳定 RunTrace 映射到 OpenTelemetry。

### Span 映射

- Run → Root Span；
- LLM → LLM Span；
- Tool → Tool Span；
- Approval → Approval Span；
- Delegation → Delegation Span。

### 隐私规则

- 默认不导出完整 Prompt；
- 默认不导出完整 Tool 输出；
- 不导出 Secret；
- 内容导出必须显式启用。

### 失败隔离

- OTLP 失败不影响 Runtime；
- RuntimeEngine 不直接依赖 OTel SDK；
- Exporter 只消费 RunTrace。

---

# 10. 阶段依赖

```text
PR1  RunEvent Read Contract
  ↓
PR2  RunTrace + TraceService + JSON Export
  ↓
PR3  EvalCase + Fixture Environment
  ↓
PR4  EvalRunner + Scorers
  ↓
PR5  TraceToEvalCaseDraft + Dataset
  ↓
PR6  Playback + Re-execution + Gate
  ↓
PR7  FailureAttributor
  ↓
PR8  OTLP Exporter
```

---

# 11. 阶段退出条件

| 阶段 | 退出条件 |
|---|---|
| Trace | 任意 Run 可动态查询稳定 Trace，终态 Run 可显式导出 JSON |
| Eval | EvalCase 可在隔离环境中重复执行并稳定评分 |
| Replay/Regression | 历史失败可进入 Dataset，并通过 Playback 进入 CI |
| Advanced | 失败可规则归因，Trace 可安全导出 OTLP |

---

# 12. 全局工程约束

## 12.1 确定性

在以下条件相同时，Playback 结果必须稳定：

- EvalCase；
- Fixture；
- Runtime 版本；
- TokenCounter；
- Scorer 配置。

时间戳和新生成 ID 必须在 Golden Test 中规范化。

## 12.2 Schema Version

以下模型独立维护 Schema Version：

- RunTrace；
- EvalCase；
- EvalResult；
- AttributionResult。

Breaking Change 必须提升主版本。

## 12.3 向后兼容

- 不强制迁移历史 `events.jsonl`；
- 历史字段缺失生成 TraceIssue；
- 不静默丢弃关键事件；
- 无法恢复的损坏必须产生明确错误。

## 12.4 失败分类

必须区分：

```text
Runtime Failure
Trace Reconstruction Failure
Fixture Configuration Failure
Eval Assertion Failure
Exporter Failure
```

Exporter Failure 不能转化为 Runtime Failure。

## 12.5 PR 边界

每个 PR：

- 只完成当前范围；
- 不引入未使用的未来抽象；
- 不顺带重构 Runtime、Tool、LLM 或 Session；
- 不修改 Journal；
- 必须独立测试；
- 必须更新对应 Wiki；
- 必须明确已完成和未完成范围。

---

# 13. 关键风险

## 13.1 RunEvent 关联数据不足

处理：

- PR1 补齐事件读取能力和审批最小字段；
- PR2 在实际配对时再判断是否需要新增其他字段；
- 历史事件生成 TraceIssue；
- 不强制迁移历史数据。

## 13.2 Trace 成为第二事实源

处理：

- RunTrace 默认动态构建；
- JSON 只显式导出；
- Runtime 不读取 `trace.json` 进行恢复；
- JSON 文件记录来源 sequence 和 record hash。

## 13.3 Playback 被误认为模型行为评测

处理：

- 明确区分 Playback 和 Re-execution；
- Playback 固定模型输出；
- Re-execution 才调用当前模型；
- PR Gate 只使用 Playback。

## 13.4 Replay 误用生产 Resume

处理：

- Replay 始终创建新 Run；
- 不接受生产 Checkpoint；
- 不调用 `retry_interrupted()`；
- 不写原 Session；
- 不执行生产副作用。

## 13.5 模块范围膨胀

处理：

- 固定四阶段、八个 PR；
- 每阶段完成退出条件后再进入下一阶段；
- OTLP 和归因不阻塞 MVP；
- 不预建没有真实使用方的抽象。

---

# 14. 最终交付边界

本项目最终分为四个部分：

```text
一、Trace
事件读取、动态轨迹组装、查询和显式导出

二、Eval
用例模型、Fixture 环境和确定性评分

三、Replay / Regression
历史轨迹转数据集、Playback、Re-execution 和 CI 门禁

四、Advanced
失败归因和 OTLP 导出
```

对应八个 PR：

```text
PR1：RunEvent Read Contract
PR2：RunTrace + TraceService + JSON Export
PR3：EvalCase + Fixture Environment
PR4：EvalRunner + Deterministic Scorers
PR5：TraceToEvalCaseDraft + Dataset
PR6：Playback + Re-execution + RegressionGate
PR7：FailureAttributor
PR8：OTLP Exporter
```

前三个阶段形成必须完成的核心开发闭环。

第四阶段属于增强能力，不得阻塞 Trace、Eval 和 Replay 的投入使用。
