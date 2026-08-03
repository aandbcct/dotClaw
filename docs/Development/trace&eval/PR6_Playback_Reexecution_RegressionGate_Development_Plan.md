# dotClaw PR6 开发计划：Playback、Re-execution 与 RegressionGate

## 1. 定位与边界

### 唯一目标

批量执行 Dataset 的 Playback 或 Re-execution，生成明确的 RegressionReport；只有确定性 Playback 影响 CI Gate。

```text
Dataset cases → Playback → EvalResult[] → RegressionReport → RegressionGate
Dataset cases → Re-execution → EvalResult[] → 比较报告（不进 Gate）
```

不包含真实副作用、生产 Resume、成本金额门禁、LLM Judge 或重新定义 PR4 的评分规则。

## 2. 文件与接口

新增：

```text
src/dotclaw/eval/
├── playback.py
├── reexecution.py
├── regression.py
└── gate.py

tests/eval/
├── test_playback.py
├── test_reexecution.py
├── test_regression.py
└── test_gate.py
```

公开接口：

```python
class PlaybackRunner:
    async def run_dataset(self, dataset_path: str | Path) -> tuple[EvalResult, ...]: ...

class ReexecutionRunner:
    async def run_dataset(self, dataset_path: str | Path) -> tuple[EvalResult, ...]: ...

class RegressionGate:
    def evaluate(self, results: tuple[EvalResult, ...]) -> RegressionReport: ...
```

`RegressionReport` 含 schema_version、dataset 标识、Case Result 摘要、overall status 和诊断信息；状态固定为 `PASS`、`REGRESSION`、`ERROR`。

## 3. 模式与判定规则

- Playback 强制 `ExecutionMode.PLAYBACK`、STRICT 匹配、冻结 Agent Policy / Context / LLM / Tool / Approval / Delegation Fixture，复用 PR3 Environment 与 PR4 EvalRunner；每个 Case 创建独立 InMemory Run。
- Re-execution 使用当前 Agent / Prompt / LLM，保留 Case Conversation 和隔离外部 Fixture；只能以 NORMAL 匹配运行，结果只供人工比较，不能调用 Gate。
- 单 Case 是否通过严格复用 `EvalResult.passed`：全部 Expectation 必须通过，不重算分数、不加阈值。
- Gate：全部可信 Playback Result 通过为 PASS；至少一个已完成 Playback 的断言失败为 REGRESSION；Dataset 读取、Fixture、Trace 重建或环境无法产生可信 Result 为 ERROR。REGRESSION 与 ERROR 都令 CI 非零退出，但报告必须区分行为回退与评测基础设施错误。
- Re-execution 的随机模型质量变化、真实网络调用尝试、Exporter 失败都不得转化为 Regression。

## 4. 规范化与安全

- 比较和报告前规范化新 Run ID、时间戳、临时目录和其他非语义生成值；保留 Case ID、Expectation、Span 证据与失败分类。
- Dataset 只加载 `cases/`，忽略 Draft；未知 / 损坏 Case 导致 ERROR，不能静默跳过。
- Playback / Re-execution 都不调用 `retry_interrupted()`、不写原 Run / Session / 工作目录、不使用生产凭证；任何外部调用未被 Fixture 覆盖均应为 ERROR。

## 5. 测试与验收

必须覆盖：

1. 多 Case 稳定 PASS、单 Case 断言失败 REGRESSION、Fixture / Dataset 错误 ERROR；
2. 相同 Dataset 与相同版本环境重复得到等价 Report；
3. 时间 / 新 ID 变化不产生 Regression；
4. Re-execution 产生比较结果但没有 Gate 入口，不能阻断；
5. 生产 Resume、Session、真实网络 / 工作目录访问的反向测试；
6. CI 调用入口对 PASS 返回成功码，对 REGRESSION / ERROR 返回失败码；
7. `pytest -q tests/eval tests/trace tests/runtime_v2`、`compileall src`、`git diff --check` 通过。

## 6. 推荐提交顺序与完成门槛

1. **Dataset 批量加载与 PlaybackRunner**；
2. **Report / Gate 三态与规范化**；
3. **Re-execution 比较路径**；
4. **CI 入口、隔离与端到端回归**。

完成后确定性 Dataset 可在 CI 阻断行为回退；Re-execution 仍仅用于人工观察。
