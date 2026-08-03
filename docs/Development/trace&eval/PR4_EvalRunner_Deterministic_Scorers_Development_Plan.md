# dotClaw PR4 开发计划：EvalRunner 与确定性 Scorer

## 1. 定位与边界

### 唯一目标

执行 `EvalCase`，为隔离 Run 构建 Trace，并用 9 个确定性 Scorer 返回可追溯 `EvalResult`。

```text
EvalCase → EvalRunner → EvalEnvironment / RuntimeEngine → RunTrace
         → Expectation[] → Scorers → EvalResult
```

不包含 Dataset、Draft、Playback 批处理、RegressionGate、LLM Judge 或改变 Runtime / Trace 以适应评分。

## 2. 文件与接口

新增：

```text
src/dotclaw/eval/
├── runner.py
├── results.py
└── scorers/
    ├── __init__.py
    ├── run_status.py
    ├── tool_sequence.py
    ├── tool_arguments.py
    ├── approval.py
    ├── policy.py
    ├── output_assertion.py
    ├── context_retention.py
    ├── token_budget.py
    └── iteration_budget.py

tests/eval/
├── test_runner.py
├── test_results.py
└── scorers/
```

公开接口：

```python
class EvalRunner:
    async def run(self, case: EvalCase) -> EvalResult: ...

class Scorer(Protocol):
    def score(self, trace: RunTrace, expectation: Expectation) -> AssertionResult: ...
```

仅在 PR4 有九个真实 Scorer 后提取这个小 Protocol；不建立 Scorer Registry。`EvalRunner` 按固定 `ExpectationKind` 分派 Scorer。

`results.py` 定义：

```text
EvaluationFailureKind: RUNTIME | TRACE_RECONSTRUCTION | FIXTURE_CONFIGURATION | ASSERTION
AssertionResult: expectation, passed, evidence
EvalResult: schema_version, case_id, run_id, passed,
            assertion_results, failure_kind?, failure_detail?, trace
```

## 3. Expectation 与九类 Scorer

每条 `Expectation` 只含 `kind`、`target`、`expected`、`options`。未知 kind、缺少字段或不合法 options 在 Runner 前校验为 FIXTURE_CONFIGURATION；每个 Scorer 只读取自身 kind。

| kind | target / expected | Trace 证据 |
|---|---|---|
| RUN_STATUS | 期望 Run / Outcome | RUN Span、AgentRun |
| TOOL_SEQUENCE | 有序 tool_name / call_id 序列 | TOOL Span |
| TOOL_ARGUMENT | call_id 或 tool_name 的关键参数子集 | TOOL Span、源 RunMessage |
| APPROVAL | call_id / approval_id 的等待与决定 | APPROVAL Span |
| POLICY | 允许 / 拒绝结果 | TOOL / APPROVAL Span |
| OUTPUT_ASSERTION | exact / contains / regex 文本 | 最终 assistant RunMessage |
| CONTEXT_RETENTION | 指定文本 / message ID 是否在目标 ContextVersion | ContextVersion |
| TOKEN_BUDGET | 最大 tokens_in / tokens_out / total | Trace Metrics 或权威 statistics |
| ITERATION_BUDGET | 最大 LLM 调用数 / loop 次数 | LLM Span、Run statistics |

- 每条 Expectation 单独产生 pass/fail 与证据。
- `EvalResult.passed` 仅当全部已配置 Expectation 通过；不做分数、权重、容错比例或多数判定。
- Runtime 执行失败、Trace 重建失败、Fixture 不匹配、断言失败必须分别表示；仅最后一类是“已可信执行但行为不符合预期”。
- PR4 允许 Case 显式声明部分 Trace 评分；未声明时部分 Trace 返回 TRACE_RECONSTRUCTION Failure。

## 4. 执行规则

1. Runner 校验 EvalCase 与 Expectation；
2. 根据 execution_mode 创建 PR3 Environment：Playback 使用冻结 Policy / Context，Re-execution 使用当前 Agent / Prompt 并保持外部隔离；
3. 执行隔离 Runtime，并按 Case 的 Approval Fixture 自动解决或按预期停在等待状态；
4. 以 PR2 TraceService / assembler 组装 Eval Run Trace；
5. 固定顺序执行 Case 中各 Expectation，汇总 AssertionResult；
6. 返回包含 Run ID、Trace 和失败分类的 EvalResult。

## 5. 测试与验收

每个 Scorer 至少有一个通过和一个失败用例；另覆盖：

- 多个 Expectation 的全通过与任一失败；
- 精确、包含、正则输出模式及非法正则配置；
- 参数子集匹配与 Tool 顺序错误；
- 上下文保留、Token 上限、迭代上限；
- Runtime Failure、Fixture Configuration Failure、Trace Reconstruction Failure、Assertion Failure 的互斥分类；
- 固定 Case / Agent / Context / Fixture / TokenCounter / Runtime 版本重复运行得到等价 Result。

完成门槛：`pytest -q tests/eval tests/trace tests/runtime_v2`、`compileall src`、`git diff --check` 通过，且每条失败证据可回溯到 Eval Run Trace。

## 6. 推荐提交顺序

1. **结果模型与 Case 校验**；
2. **Runner 主链与失败分类**；
3. **行为 Scorer**：Status、Tool、Approval、Policy、Output；
4. **上下文与预算 Scorer**：Context、Token、Iteration；
5. **重复性与端到端回归测试**。

完成后可以对手写 EvalCase 确定性评分；历史 Trace 转 Case 与批量 Gate 仍属于后续 PR。
