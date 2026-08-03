# dotClaw PR7 开发计划：FailureAttributor

## 1. 定位与边界

### 唯一目标

只基于 `RunTrace` 和 `EvalResult`，以固定普通规则找出最早且足以决定失败的证据。

不读取 Runtime 文件、不写 Event、不调用 LLM、不构建规则引擎或改变 Gate 真值。

## 2. 文件与接口

新增：

```text
src/dotclaw/eval/
├── attribution.py
└── attribution_rules.py

tests/eval/
└── test_attribution.py
```

`attribution.py` 定义：

```python
class FailureAttributor:
    def attribute(self, trace: RunTrace, result: EvalResult) -> AttributionResult: ...
```

`AttributionResult` 字段：schema_version、category、confidence、decisive_span_id、evidence、secondary_causes。`confidence` 仅为 HIGH / MEDIUM；UNKNOWN 不伪造 LOW。

## 3. 固定规则

`attribution_rules.py` 使用有序普通函数或条件表，不引入 DSL / 注册表。扫描 Trace 的时间 / sequence 顺序，首个有充分证据的命中即为主因；之后只收集不改变主因的次要原因。

固定类别：

```text
CONTEXT_BUILD_FAILURE              CONTEXT_BUDGET_EXCEEDED
CONTEXT_INFORMATION_LOST           LLM_UNAVAILABLE
LLM_INVALID_ACTION                 WRONG_TOOL_SELECTED
TOOL_ARGUMENT_INVALID              TOOL_EXECUTION_FAILED
POLICY_DENIED                      UNNECESSARY_APPROVAL
APPROVAL_REJECTED                  DELEGATION_FAILED
GOAL_NOT_COMPLETED                 ITERATION_BUDGET_EXCEEDED
TOKEN_REGRESSION                   UNKNOWN
```

- HIGH：Trace 和 EvalResult 对同一失败有直接证据；MEDIUM：只有 Trace 事实支持；没有充分证据返回 UNKNOWN。
- 归因只解释现有事实和确定性 Assertion；不会把 `TraceIssue` 解释成自然语言根因。
- Dataset / Fixture Configuration Failure 属于评测基础设施，`attribute()` 不伪装为 Agent 归因，应返回 UNKNOWN 并保留原失败分类给调用者。

## 4. 测试与验收

- 每个类别有最小正例和 evidence 断言；
- 多个异常时选最早决定性 Span，而非枚举顺序或最后一个错误；
- Trace + Result 直接证据为 HIGH，仅 Trace 为 MEDIUM，证据不足与基础设施错误均稳定 UNKNOWN；
- Attribution 输入 / 输出纯内存，无 Runtime 文件 I/O；
- `pytest -q tests/eval tests/trace`、`compileall src`、`git diff --check` 通过。

## 5. 推荐提交顺序与完成门槛

1. **结果模型与 UNKNOWN / 置信度边界**；
2. **Context / LLM / Tool / Policy 规则**；
3. **Approval / Delegation / Goal / Budget 规则**；
4. **多故障排序与回归矩阵**。

完成后 FailureAttributor 只产生可审计诊断；它不改变 EvalResult、RegressionGate 或 Runtime。
