# dotClaw PR8 开发计划：OTLP Exporter

## 1. 定位与边界

### 唯一目标

将终态完整 `RunTrace` 显式映射到 OpenTelemetry，而不让 OTel SDK 或自动上报进入 Runtime。

不包含实时前端观测、部分 Trace 上报、Runtime 自动 Trace、Trace 写回或 OTel 作为事实源。

## 2. 文件与接口

新增：

```text
src/dotclaw/trace/exporters/
└── otlp_exporter.py

tests/trace/
└── test_otlp_exporter.py
```

依赖声明仅增加官方 OpenTelemetry API / SDK 及测试用内存 Exporter；SDK import 只能出现在 `trace/exporters/otlp_exporter.py` 和对应测试。

公开接口：

```python
class OtlpTraceExporter:
    def export(self, trace: RunTrace, *, include_content: bool = False) -> OtlpExportResult: ...
```

- `export()` 仅接受终态且 `is_partial=False` 的 Trace；否则抛明确 `ValueError`，不补造结束时间。
- 调用方显式调用；任何 OTel 异常转换为 Exporter Failure 返回 / 异常，绝不改变 Runtime、Trace、Eval 或 Gate。

## 3. 映射与隐私

| TraceSpan | OTLP Span |
|---|---|
| RUN | root span |
| LLM / TOOL / APPROVAL / DELEGATION | RUN 的子 span，按 parent_span_id 保持层级 |

- Span 起止时间直接取 Trace；FAILED 映射 OTel ERROR，其余终态映射非错误状态；属性使用 Trace 已知 attributes、run_id、schema version、record_hash 与 event sequence，不增加消息副本。
- 默认不导出 Prompt、模型正文、完整工具输出、Secret 或 Draft 内容；`include_content=True` 才允许导出与 PR2 JSON Export 同样已明确允许的内容。
- 导出前复用同一脱敏规则；不得在 OTel 属性中泄漏原始敏感字段。

## 4. 测试与验收

用内存 OTel Exporter 验证：

1. RUN 根与四类子 Span 的名称、层级、起止时间、状态和最小属性；
2. 默认属性不含 Prompt、模型正文、工具输出或 Secret；显式内容模式仍经脱敏；
3. 部分、非终态或 INCOMPLETE Trace 被拒绝；
4. SDK / Exporter 抛错时仅报告 Exporter Failure，原 Trace 和 EvalResult 不变；
5. Runtime 包不 import OTel；
6. `pytest -q tests/trace tests/eval tests/runtime_v2`、`compileall src`、`git diff --check` 通过。

## 5. 推荐提交顺序与完成门槛

1. **依赖与最小 RUN / 子 Span 映射**；
2. **状态、属性与脱敏**；
3. **部分 Trace 拒绝和失败隔离**；
4. **内存 Exporter、依赖方向与全量回归**。

完成后调用方可以显式导出稳定 Trace；Runtime 仍不知道 OTel 存在。
