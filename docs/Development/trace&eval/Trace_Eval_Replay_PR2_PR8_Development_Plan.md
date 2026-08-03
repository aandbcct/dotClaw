# dotClaw Trace、Eval 与 Replay：PR2–PR8 开发计划

## 0. 文档信息

| 项目 | 内容 |
|---|---|
| 文档状态 | 已确认开发计划 |
| 上游基准 | `Trace_Eval_Replay_PRD_v2.1.md`、`PR1_RunEvent_Read_Contract.md` |
| 覆盖范围 | PR2–PR8 |
| 不包含 | PR1 实现、Runtime 状态机改造、Journal、生产恢复替换 |

本文档是 PR2–PR8 的唯一计划基线。讨论中确认的决定只在本文原地收敛；未确认的实现选择不写入本文。

---

## 1. 已确认的全局边界

### 1.1 概念与依赖方向

```text
Runtime 权威事实（AgentRun / RunEvent / RunMessage / ContextVersion）
        ↑ 只读
Trace（RunTrace 组装、查询、导出）
        ↑ 使用
Eval（Case、Fixture、Runner、Scorer、Dataset、Gate）
```

- Runtime 不依赖 Trace 或 Eval；Trace 不修改 Runtime 事实；Eval 可以调用隔离 RuntimeEngine 和 Trace。
- `RunTrace` 是动态构建的派生读模型；JSON Trace 只是显式导出工件，不能作为恢复或后续查询的权威来源。
- `RunCheckpoint` 与 `ApprovalRecord` 属于恢复控制记录，不是普通 Trace 的必要输入。
- Playback 和 Re-execution 都创建新的隔离 Run；不使用生产 Resume，不修改原 Run、生产 Session 或生产目录。
- Playback 冻结 Case 中的 Agent 策略、上下文和全部外部 Fixture，保证 CI 可重复；Re-execution 使用当前 Agent / Prompt 与当前 LLM，但仍使用 Case 的会话 Fixture 和隔离外部依赖，结果不进入强制 Gate。
- 前端运行进度直接消费 `AgentRun` 与已持久化 `RunEvent`；不以 Trace 作为实时 UI 的必经层。Trace 的主要消费者是 Eval、Replay、显式导出与离线诊断，本计划不新增前端页面、实时订阅或推送能力。

### 1.2 已确认的读取与失败边界

- PR1 负责拒绝结构损坏：JSON、字段类型、sequence 和 run_id 不合法时读取失败。
- PR2 负责解释语义不完整：缺少配对事件、缺少历史字段、未知消息引用或无法关联 ContextVersion 时，构建 `TraceIssue`；不把可诊断的历史不完整直接转换为读取异常。
- `TraceIssue` 只保存机器可判断的结构化事实：问题类别、关联的 event/message/span 标识和简短证据；不在 Trace 层推断自然语言根因。最早决定性原因与根因分类属于 PR7 `FailureAttributor`。
- 非终态 Run 可查询：Trace 标记 `is_partial=true`，来源 sequence 表示读取时已持久化的连续事件前缀；不承诺跨 `run.json`、`messages.json`、`checkpoint.json` 的事务快照。
- Exporter、Trace 重建、Fixture 配置与 Eval Assertion 必须和 Runtime Failure 分开报告；Exporter 失败不得改变 Run 结果。

### 1.3 全局非目标

- 不改造 Journal，不创建第二套事实源，不新增自动 Trace 持久化。
- V1 不引入 LLM Judge、真实副作用回放或金额成本门禁。
- 未出现第二个真实调用方前，不新增通用 Exporter Port、Repository、Registry 或 Service 层。

---

## 2. 交付顺序与阶段退出条件

| 阶段 | PR | 本阶段唯一结果 | 退出条件 |
|---|---|---|---|
| Trace | PR2 | 任意 Run 可动态得到稳定 Trace | 终态 Trace 可显式导出 JSON；不完整记录有 Issue |
| Deterministic Eval | PR3、PR4 | Case 在隔离 Runtime 中可重复执行并评分 | 相同 Fixture、Runtime 版本和 Scorer 配置得到稳定结果 |
| Replay / Regression | PR5、PR6 | 历史失败可审核、可 Playback、可作为 Gate 输入 | CI 只以确定性 Playback 决定通过与否 |
| Advanced | PR7、PR8 | Trace 可归因并安全映射至 OTLP | 两项均只消费既有 Trace / EvalResult，不阻塞 MVP |

PR2、PR3、PR4、PR5、PR6 按依赖顺序完成；PR7 与 PR8 在 PR6 后互不依赖，可分别排期。

### 2.1 代码交付 PR 划分

开发按四个阶段各开一个交付 PR，而不是为每份实施计划单独开 PR：

| 交付 PR | 阶段 | 包含的实施计划 | 合并前退出条件 |
|---|---|---|---|
| 阶段一 PR | Trace | PR1 + PR2；若 PR1 已单独合入，则仅 PR2 | 动态 Trace、显式 JSON 导出与 Golden Trace 通过 |
| 阶段二 PR | Deterministic Eval | PR3 + PR4 | 隔离 Case 可重复执行，9 个确定性 Scorer 可追溯评分 |
| 阶段三 PR | Replay / Regression | PR5 + PR6 | 审核 Case 可进入 Dataset，Playback 可产生稳定 Gate 结论 |
| 阶段四 PR | Advanced | PR7 + PR8 | 规则归因与显式 OTLP 导出均不影响 Runtime / Eval 真值 |

每份 PR 实施计划仍是阶段内的提交与测试边界：先完成其前置契约和测试，再做后续能力；不在阶段内以一次不可定位的大改动完成。未达到本阶段退出条件前不开始下一阶段。

对应的可实施计划：

| PR | 详细计划 |
|---|---|
| PR2 | [RunTrace、TraceService 与 JSON 导出](./PR2_RunTrace_TraceService_JSON_Development_Plan.md) |
| PR3 | [EvalCase 与 Fixture Environment](./PR3_EvalCase_Fixture_Environment_Development_Plan.md) |
| PR4 | [EvalRunner 与确定性 Scorer](./PR4_EvalRunner_Deterministic_Scorers_Development_Plan.md) |
| PR5 | [TraceToEvalCaseDraft 与 Dataset](./PR5_TraceToEvalCaseDraft_Dataset_Development_Plan.md) |
| PR6 | [Playback、Re-execution 与 RegressionGate](./PR6_Playback_Reexecution_RegressionGate_Development_Plan.md) |
| PR7 | [FailureAttributor](./PR7_FailureAttributor_Development_Plan.md) |
| PR8 | [OTLP Exporter](./PR8_OTLP_Exporter_Development_Plan.md) |

---

## 3. PR2：RunTrace、TraceService 与 JSON 导出

### 3.1 唯一目标

从 `RunRepository` 读取一次 Run 的权威事实，动态组装供 Eval、Replay、显式导出与离线诊断使用的版本化 `RunTrace`；不写入 Runtime，也不承担前端实时进度展示。

```text
run_id → TraceService → AgentRun + Event[] + Message[] + ContextVersion[]
       → assemble_trace() → RunTrace → JsonTraceExporter
```

### 3.2 模块与职责

```text
src/dotclaw/trace/
├── models.py          # RunTrace、TraceSpan、TraceIssue、TraceMetrics 与 schema version
├── assembler.py       # 唯一的事实解释与 Span 配对入口
├── service.py         # 仅定位 Run 并加载四类权威事实
└── exporters/
    └── json_exporter.py  # 显式序列化 RunTrace
```

- `TraceService` 不解释事件，不创建 `RunRecord` / `RunRecordReader` 中间模型。
- `assemble_trace()` 不读文件、不调用 Runtime、不写出工件；只将已加载事实转为读模型。
- Trace V1 不创建事件图、规则引擎、注册表或插件机制；只使用满足当前查询、导出、Eval 与 Replay 输入所需的模型和函数。
- `RunTrace` 直接保留既有 `RunMessage[]` 与 `ContextVersion[]`；`TraceSpan` 仅保存对 message ID 和 ContextVersion 的引用，不复制或重新定义 `TraceMessage`、`TraceContext` 等平行模型。
- `TraceSpan` 使用统一模型，不按 LLM、Tool、Approval、Delegation 再拆分子类。V1 固定字段为 `span_id`、`kind`、`parent_span_id`、`started_at`、`ended_at`、`status`、`start_event_sequence`、`end_event_sequence`、`message_ids`、`context_version` 与 `attributes`；`attributes` 只存已知必要事实（如 model_id、call_id、tool_name、approval_id、child_run_id），不作为开放扩展机制。
- `status` 使用固定枚举 `COMPLETED`、`FAILED`、`CANCELLED`、`WAITING`、`INCOMPLETE`；不增加“重试中”“部分成功”等细分状态，细节保留在 `attributes` 与 `TraceIssue`。
- V1 Span 仅有 `RUN`、`LLM`、`TOOL`、`APPROVAL`、`DELEGATION`；ContextVersion 是 LLM Span 的关联事实，不创建 `CONTEXT` Span；不推测 Compaction Span。
- `record_hash` 仅由权威输入计算，不含 `assembled_at`；同一输入的 Trace 语义等价，Golden Test 中规范化时间戳和新 ID。

### 3.3 组装与不完整记录

- 按 Event sequence 配对 LLM、Tool、Approval、Delegation 的开始/完成过程；LLM 依当前串行调用约束按事件顺序配对。
- 关联 `RunMessage` 中的工具参数、工具结果与 `ContextVersion`；缺失或冲突形成 `TraceIssue`，不静默丢弃关键事件。
- 非终态 Run、未配对 Span 或必要关联缺失均令 `is_partial=true`；结构合法但没有事件的 Run 可返回仅含 RUN Span 的部分 Trace。
- 若 PR2 的配对测试证明现有事件信息无法无歧义关联，停止在该证据处，不顺带扩展 Engine；把最小字段补充作为单独确认的 Runtime 变更。

### 3.4 导出与测试

- `JsonTraceExporter` 仅接收 `RunTrace`；默认只导出终态 Trace，调试导出部分 Trace 必须显式允许；同一目标文件允许显式覆盖。默认导出结构、Span、元数据、message ID、ContextVersion 引用与脱敏摘要，不导出完整 Prompt、模型正文、工具输出或疑似 Secret；只有显式 `include_content=true` 才导出完整内容。`record_hash` 始终由原始权威事实计算，不因导出脱敏而改变。
- Golden fixtures 覆盖：纯 LLM、Tool 成功/失败、审批通过/拒绝、Delegation、运行中 Trace、历史缺字段、缺配对或缺引用。
- 验收：查询不产生文件；导出不改变 Runtime；相同权威输入产生稳定语义；所有 Issue 可定位其证据。

---

## 4. PR3：EvalCase 与 Fixture Environment

### 4.1 唯一目标

定义可版本化 `EvalCase`，并为其提供默认拒绝真实依赖的隔离执行环境。

```text
EvalCase → Fixture Environment → 隔离 RuntimeEngine → Eval Run
```

### 4.2 最小模型与环境

```text
src/dotclaw/eval/
├── models.py       # EvalCase 与 Fixture 事实
├── fixtures.py     # ScriptedLLMPort、FixtureToolPort、Fixture DelegationPort、FixtureRunPolicyPort、FixtureContextPort
└── environment.py  # 隔离 Repository、Checkpoint、TokenCounter、HistoryCompactor 的组装
```

- `EvalCase` 包含：case_id、schema_version、name、agent_id、input、conversation_fixture、policy_fixture、context_fixtures、llm_fixture、tool_fixtures、expectations、tags、source_trace、execution_mode。
- `expectations` 是有序 `Expectation[]`。每项只包含 `kind`、`target`、`expected` 与必要的 `options`；`kind` 对应一个确定性 Scorer，Scorer 只读取并校验属于自己的 Expectation。不得为 9 个 Scorer 建立平行的 Case 配置模型。
- `policy_fixture` 冻结 Playback 所需的 Agent 策略；`context_fixtures` 按模型调用顺序冻结上下文构建结果。二者复用现有 Runtime DTO / 事实结构，不新增平行 Agent 或 Context 领域模型。
- Fixture 默认拒绝未匹配调用，绝不回退真实 LLM、Tool、网络、生产凭证、生产 Session 或工作目录。
- 支持 NORMAL 与 STRICT 匹配模式；审批和委派均使用 Fixture，隔离 Repository 与 CheckpointRepository 只服务 Eval Run。STRICT 按记录调用顺序精确匹配 LLM、Tool、Approval 与 Delegation，额外、缺失或参数不一致均失败；它用于 Playback 与 CI Gate。NORMAL 仍拒绝未配置调用，但 Tool 只匹配名称和声明的关键参数，允许未声明的非关键参数差异；它只用于 Re-execution 观察，不进入 Gate。Playback 使用 FixtureRunPolicyPort 与 FixtureContextPort；Re-execution 改用当前 Agent / Prompt 的 Policy 与 Context 组装，但仍不得访问生产 Session、Memory 或外部网络。
- 本 PR 不定义 Scorer、Dataset、Trace 自动转 Case 或 Gate。

### 4.3 测试与验收

- 覆盖 Policy、Context、LLM、Tool、Approval、Delegation Fixture 的匹配、未匹配拒绝、调用顺序与隔离目录。
- 覆盖隔离环境绝不访问生产 Port / Session 的反向测试。
- 验收：同一 Case 的 Fixture 路径可重复驱动 Runtime，且不会产生真实副作用。

---

## 5. PR4：EvalRunner 与确定性 Scorer

### 5.1 唯一目标

执行 `EvalCase`，为新的隔离 Run 构建 Trace，并以确定性规则产生 `EvalResult`。

```text
EvalCase → EvalRunner → 隔离 RuntimeEngine → RunTrace → Scorers → EvalResult
```

### 5.2 职责边界

```text
src/dotclaw/eval/
├── runner.py
├── results.py
└── scorers/
    ├── run_status.py
    ├── tool_sequence.py
    ├── tool_arguments.py
    ├── approval.py
    ├── policy.py
    ├── output_assertion.py
    ├── context_retention.py
    ├── token_budget.py
    └── iteration_budget.py
```

- `EvalRunner` 按 execution_mode 选择 Playback 的冻结 Policy/Context Fixture，或 Re-execution 的当前 Agent / Prompt；随后创建隔离 Run、处理 Case 显式声明的审批 Fixture、调用 TraceService/Assembler，并汇总 Scorer 输出。
- PR4 一次实现 PRD 定义的 9 个确定性 Scorer：`RunStatus`、`ToolSequence`、`ToolArgument`、`Approval`、`Policy`、`OutputAssertion`、`ContextRetention`、`TokenBudget` 与 `IterationBudget`。每个 Scorer 只输出可追溯的 pass/fail 与证据，不引入加权评分框架或 LLM Judge。
- 每个 Scorer 只消费 `Expectation.kind` 与自身匹配的条目；Case 中未知 kind、缺失必需字段或 options 不合法属于 Fixture Configuration Failure，而不是 Runtime 或 Assertion Failure。
- 每条 Expectation 产生一个 pass/fail 与证据；`EvalResult.passed` 当且仅当该 Case 的全部已配置 Expectation 都通过。不做加权总分、容错比例或“多数通过即成功”；需要宽松判断时由 Case 不配置相应 Expectation。
- `EvalResult` 独立维护 schema version，区分 Runtime、Trace、Fixture 配置和 Assertion 失败。
- 不通过修改 Runtime 或 Trace 来使评分通过。

### 5.3 测试与验收

- 固定 Case、冻结 Agent 策略、Context / 外部 Fixture、Runtime 版本、TokenCounter 与 Scorer 配置，重复执行得到等价 Result。
- 覆盖全部 9 类 Scorer 的成功与失败、Fixture 未匹配、Runtime 失败、部分 Trace 评分（仅 Case 显式允许）与审批自动解决。
- 验收：所有分数和失败证据可由 Eval Run Trace 追溯。

---

## 6. PR5：TraceToEvalCaseDraft 与 Dataset

### 6.1 唯一目标

将终态 `RunTrace` 转为需人工审核的 `EvalCaseDraft`，并以版本化 Dataset 保存审核后的 Case。

```text
终态 RunTrace → TraceToEvalCaseDraft → 人工审核 → EvalCase → Dataset
```

### 6.2 边界

- 提取用户输入、冻结 Agent 策略与上下文、Conversation、LLM 输出、Tool 调用、审批和委派 Fixture，并生成基础 Expectations。
- Draft 保留 source trace、source record hash、转换时的 schema 信息与待审核 `Expectation[]`；审核后才是可执行 `EvalCase`。
- Dataset 是一个目录：`datasets/<dataset_name>/drafts/<draft_id>.draft.json` 保存可长期保留的多个待审 Draft，`datasets/<dataset_name>/cases/<case_id>.json` 保存已审核 Case。每个工件独立保存为 JSON，加载 Case 时按稳定文件名排序；tags、schema version、来源 Trace 与去重信息均保存在 Case 内，不新增 Manifest、数据库、Registry 或 Dataset Repository。
- `EvalCaseDraftService` 是供 Channel 调用的窄应用接口，而非 Runtime 主流程。它至少提供 `load_draft(dataset_name, draft_id)`、`save_reviewed_draft(dataset_name, draft_id, draft)` 与 `confirm_draft(dataset_name, draft_id, case_id)`；后者校验已审核 Draft，原子写入对应 Case，并在 Draft 中记录已确认的 case_id，不删除 Draft。Channel 不直接读写 Dataset 文件。
- ApplicationHost 负责组装并向 Channel 注入 `EvalCaseDraftService`；本 PR 不新增 Runtime Port、通用审核服务或状态机。
- Trace 转 Draft 时递归脱敏已知敏感字段名（如 token、api_key、password、authorization、cookie、secret）与常见凭证模式（如 Bearer Token、私钥块、常见 API Key 格式）。机器无法可靠判断任意自然语言是否敏感，因此自动检测只提供已知模式保护，不宣称完整识别。
- LLM 回复作为可回放 Fixture 内容，默认经过同一脱敏器处理，但其存在本身不触发 `requires_review`，避免每个普通 Case 都需要额外敏感内容审核。只有自动脱敏无法安全处理的敏感载荷才标记 `requires_review=true`；人工必须经 Channel 审阅、移除或替换该内容，并以 `save_reviewed_draft()` 显式确认审核完成后才能清除此标记；`confirm_draft()` 拒绝仍需审核的 Draft。不实现自动猜测或替换可回放 Fixture 的逻辑。
- 部分 Trace、自动生成但未审核 Draft 不得进入 Dataset。
- 本 PR 不执行 Case，不创建 Gate，不调用真实 LLM。

### 6.3 测试与验收

- 覆盖默认 JSON 导出不含正文与 `include_content=true` 的显式完整导出；以及正常终态 Trace、含工具/审批/委派 Trace、部分 Trace 拒绝、多个 Draft 共存、已知敏感字段和凭证模式脱敏、普通 LLM 回复不触发敏感内容审核、无法安全处理的载荷需审核、未经 `save_reviewed_draft()` 的 Draft 拒绝确认、重复 Case、重复确认、Dataset schema 不兼容与 Channel 经服务确认 Draft。
- 验收：输入相同 Trace 时 Draft 稳定；审核后 Case 可被 PR4 Runner 读取执行。

---

## 7. PR6：Playback、Re-execution 与 RegressionGate

### 7.1 唯一目标

对 Dataset 执行确定性 Playback 或受控 Re-execution，并只以 Playback 结果生成 CI Gate 结论。

```text
Dataset → Playback（冻结 Agent / Context / Fixture）→ EvalResult[] → RegressionReport → RegressionGate
Dataset → Re-execution（当前 Agent / Prompt / LLM + Fixture Tool）→ 比较报告
```

### 7.2 边界

- Playback 复用 PR3 Fixture Environment 与 PR4 EvalRunner，不调用生产 Resume；冻结 Agent 策略、上下文、LLM 输出和 Tool Fixture，进入 PR Gate。
- Re-execution 使用当前 Agent / Prompt / LLM；会话与外部能力仍使用隔离 Fixture，结果仅供比较与人工判断，不阻塞 PR。
- RegressionGate 比较确定性 Scorer 指标：状态、工具路径、关键参数、审批 / Policy 行为和确定性输出断言；不判断费用金额。
- RegressionGate 只输出 `PASS`、`REGRESSION`、`ERROR`：所有 Playback 断言通过为 PASS；已完成 Playback 的确定性断言失败为 REGRESSION；Dataset、Fixture、Trace 重建或隔离执行环境无法产生可信 Result 为 ERROR。REGRESSION 与 ERROR 都使 CI 失败，但报告必须区分行为回退与评测基础设施错误。
- Playback 的单 Case 是否通过严格复用 PR4 `EvalResult.passed` 全部 Expectation 规则；RegressionReport 不重新计算分数或引入容错阈值。
- 不把模型质量的随机变化、真实网络调用或 Exporter 失败写成 Gate 失败。

### 7.3 测试与验收

- 覆盖 Playback 稳定通过、确定性退化阻断、Fixture 配置失败、Re-execution 报告但不阻断、时间和生成 ID 规范化。
- 验收：同 Dataset 在同一版本环境中生成稳定 Gate 结论，且不触碰生产 Run/Session。

---

## 8. PR7：FailureAttributor

### 8.1 唯一目标

只依据 `RunTrace` 和 `EvalResult`，按规则返回最早且足以决定失败的归因。

### 8.2 边界

- 输出 `AttributionResult(category, confidence, decisive_span_id, evidence, secondary_causes)`，独立维护 schema version。
- V1 使用固定类别：`CONTEXT_BUILD_FAILURE`、`CONTEXT_BUDGET_EXCEEDED`、`CONTEXT_INFORMATION_LOST`、`LLM_UNAVAILABLE`、`LLM_INVALID_ACTION`、`WRONG_TOOL_SELECTED`、`TOOL_ARGUMENT_INVALID`、`TOOL_EXECUTION_FAILED`、`POLICY_DENIED`、`UNNECESSARY_APPROVAL`、`APPROVAL_REJECTED`、`DELEGATION_FAILED`、`GOAL_NOT_COMPLETED`、`ITERATION_BUDGET_EXCEEDED`、`TOKEN_REGRESSION`、`UNKNOWN`。
- 不实现规则引擎：按 Trace 时间顺序寻找最早的决定性失败证据，命中一个固定类别后返回该 Span、事实证据与可选次要原因。`confidence` 只有 `HIGH`（Trace 与 EvalResult 均有直接证据）和 `MEDIUM`（仅 Trace 事实支持）；证据不足直接返回 `UNKNOWN`，不设置 LOW。
- 不读取 Runtime 文件、不修改 Event、不调用 LLM 决定 CI 真值；LLM 若后续用于解释，也只能是展示层能力。

### 8.3 测试与验收

- 每个固定类别具备最小正例；多个异常同时出现时选择最早的决定性 Span；Trace 与 EvalResult 同时支持时为 HIGH，仅 Trace 支持时为 MEDIUM，证据不足稳定返回 `UNKNOWN`。
- 验收：每项归因可回溯至 Trace Span 与 EvalResult 证据。

---

## 9. PR8：OTLP Exporter

### 9.1 唯一目标

将稳定 `RunTrace` 映射为 OpenTelemetry Span，而不让 OTel SDK 进入 Runtime。

### 9.2 边界

- `RUN` 映射根 Span；LLM、Tool、Approval、Delegation 映射子 Span。
- 默认不导出完整 Prompt、工具输出或 Secret；内容导出需显式启用。
- OTel SDK 仅位于 Trace Adapter；Exporter 接收 `RunTrace`，失败隔离并报告 Exporter Failure。
- OTLP V1 只接受终态且 `is_partial=false` 的 RunTrace，并由调用方显式触发；部分 Trace 只允许本地查询、显式 JSON 调试导出或 Eval 明确允许的评分场景，OTLP Exporter 必须拒绝它们，避免伪造结束时间或完成状态。
- 不实现 Runtime 自动上报、Trace 写回或 OTel 作为事实源。

### 9.3 测试与验收

- 用内存 OTel Exporter 断言层级、时间、属性和脱敏默认值；覆盖部分 Trace 被拒绝与 Exporter 失败。
- 验收：导出失败不改变 Runtime、Trace 或 Eval 结果。

---

## 10. 跨 PR 验证与文档收口

- 每个 PR 运行其新增单元/集成测试，并回归 `tests/runtime_v2` 中受影响的持久化、审批、恢复和委派路径。
- 每个 PR 完成后执行 `compileall`、目标 pytest、`git diff --check`；不把未训练或未验证的 Re-execution 表现称为通过。
- Runtime Wiki 仅在 PR1–PR2 影响已实现运行事实读取时增量更新；Trace/Eval Wiki 在各模块行为已由测试验证后更新。
- 最终验收覆盖：Runtime 无 Trace/Eval 依赖；Trace 不成为事实源；Eval 默认无真实副作用；Gate 只依赖确定性 Playback；Advanced 不阻塞 MVP。

---

## 11. 已确认结论

Trace 只服务 Eval、Replay、导出和离线诊断，不承担实时前端进度；`TraceIssue` 只记录结构化事实；TraceSpan 使用最小统一字段与五种状态；Playback 冻结 Agent 策略、上下文与外部 Fixture，并以 STRICT 匹配进入 Gate；Re-execution 使用当前 Agent / Prompt / LLM、NORMAL 匹配与隔离外部依赖，不进入 Gate。PR2–PR8 的实施以本文为准；后续实现中发现权威事实不足时，先以测试证明缺口，再单独确认最小 Runtime 变更。
