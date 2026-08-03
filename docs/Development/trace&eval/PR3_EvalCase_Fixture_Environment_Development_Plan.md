# dotClaw PR3 开发计划：EvalCase 与 Fixture Environment

## 1. 定位与边界

### 唯一目标

定义版本化 `EvalCase`，构造默认拒绝真实依赖的隔离 Runtime 执行环境；本 PR 不评分。

```text
EvalCase → EvalEnvironment → 隔离 RuntimeEngine → Eval Run
```

### 包含与不包含

包含 Case / Fixture 模型、STRICT / NORMAL 匹配、冻结 Playback Policy / Context、隔离 Repository / Checkpoint / TokenCounter / HistoryCompactor，以及 LLM、Tool、Approval、Delegation Fixture。

不包含 Scorer、EvalResult、Dataset、Draft、Gate、真实 LLM、真实网络、生产 Session / Memory / 工作目录访问。

## 2. 文件、模型与接口

新增：

```text
src/dotclaw/eval/
├── __init__.py
├── models.py
├── fixtures.py
└── environment.py

tests/eval/
├── test_models.py
├── test_fixtures.py
└── test_environment.py
```

`models.py` 定义：

```text
ExecutionMode: PLAYBACK | REEXECUTION
FixtureMatchMode: STRICT | NORMAL
Expectation: kind, target, expected, options
EvalCase: case_id, schema_version, name, agent_id, input,
          conversation_fixture, policy_fixture, context_fixtures,
          llm_fixture, tool_fixtures, approval_fixtures,
          delegation_fixtures, expectations, tags, source_trace,
          execution_mode
```

- `Expectation` 只是 Case 的通用断言载体；PR3 仅校验其 JSON 兼容性，具体 kind 校验留给 PR4。
- `policy_fixture` 使用现有 `AgentPolicySnapshot`；`context_fixtures` 使用现有 Runtime Context DTO 的可序列化事实，不创建平行 Agent / Context 模型。
- Case、Fixture 都提供严格 `to_dict()` / `from_dict()`；未知 schema version、重复 fixture ID、空 case_id / agent_id、非 JSON options 必须明确失败。

`fixtures.py` 提供：

```text
ScriptedLLMPort
FixtureToolPort
FixtureDelegationPort
FixtureRunPolicyPort
FixtureContextPort
FixtureApprovalRepository
```

`environment.py` 只提供具体 `EvalEnvironment(case, mode, dependencies)` 组装，不抽象新的 Environment Port。它创建 InMemoryRunRepository、InMemoryCheckpointRepository、固定 TokenCounter、固定 HistoryCompactor 和 RuntimeEngine。

## 3. 匹配与隔离规则

- 所有 Fixture 默认拒绝未匹配调用，绝不回退真实实现。
- **STRICT**：按记录顺序精确消费 LLM、Tool、Approval、Delegation Fixture；额外、缺失、调用顺序错误或参数不一致均抛出 Fixture Configuration Failure。只供 Playback / Gate。
- **NORMAL**：仍要求每个外部调用有 Fixture；Tool 按 tool_name 和 Case 声明的关键参数匹配，允许未声明非关键参数变化。只供 Re-execution，绝不进 Gate。
- Playback 使用 FixtureRunPolicyPort 与 FixtureContextPort，冻结 Agent 策略与每次上下文构建结果。
- Re-execution 可接入当前 Agent / Prompt / LLM，但 Case conversation 与所有外部能力继续隔离；任何生产 Session、Memory、网络、凭证或工作目录访问都必须由反向测试拦截。
- 审批 Fixture 只驱动隔离 Run 的审批记录；Delegation Fixture 不创建真实子 Session / 子 Run。

## 4. 测试与验收

必须覆盖：

1. EvalCase / Fixture 的序列化、schema、重复 ID 和非法值；
2. STRICT 对 LLM、Tool、Approval、Delegation 的顺序、参数、额外与缺失调用拒绝；
3. NORMAL 允许未声明 Tool 参数差异但拒绝未知 Tool；
4. 固定 Policy / Context Fixture 能驱动多轮 LLM；
5. 每种真实 Port 都配置“被调用即失败”的替身，证明环境不回退生产依赖；
6. 两次独立 Environment 不共享 Run、Checkpoint、Fixture 消费游标或目录；
7. `compileall src`、`pytest -q tests/eval tests/runtime_v2`、`git diff --check` 通过。

## 5. 推荐提交顺序与完成门槛

1. **Case / Fixture 数据模型**：schema 与 JSON 校验测试；
2. **Fixture Port**：先 LLM / Tool，再 Approval / Delegation / Policy / Context；
3. **环境组装与隔离**：InMemory 事实容器和反向访问测试；
4. **匹配回归**：STRICT / NORMAL、多轮调用、并发环境隔离。

完成后开发者可以构造隔离 Eval Run，但尚不能获得 `EvalResult`、Dataset 或 CI Gate。
