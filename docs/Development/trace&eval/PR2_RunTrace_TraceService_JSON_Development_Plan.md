# dotClaw PR2 开发计划：RunTrace、TraceService 与 JSON 导出

## 1. 定位与边界

### 唯一目标

从 PR1 提供的 Runtime 权威事实动态构建 `RunTrace`，供 Eval、Replay、显式 JSON 导出和离线诊断使用。

```text
run_id → TraceService → Run / Event[] / Message[] / ContextVersion[]
       → assemble_trace() → RunTrace → JsonTraceExporter
```

### 包含

- `src/dotclaw/trace/` 的最小模型、组装函数、读取服务和 JSON Exporter；
- RUN、LLM、TOOL、APPROVAL、DELEGATION Span；
- 语义不完整的 `TraceIssue`、部分 Trace、派生 Metrics；
- Golden Trace 与 Runtime 读取回归测试。

### 不包含

- Eval、Dataset、Playback、OTLP、前端进度、订阅或自动导出；
- 新 Runtime Port、`RunRecord` / `RunRecordReader`、事件图、规则引擎；
- Runtime Engine 字段扩展。若配对测试证明字段不足，记录证据并停止本 PR。

## 2. 文件与接口

新增：

```text
src/dotclaw/trace/
├── __init__.py
├── models.py
├── assembler.py
├── service.py
└── exporters/
    ├── __init__.py
    └── json_exporter.py

tests/trace/
├── test_assembler.py
├── test_service.py
└── test_json_exporter.py
```

修改：`docs/wiki/Runtime 模块总体说明.md`，仅说明 RunEvent 已可读取、Trace 是外部只读消费者；不重写 Runtime Wiki。

公开接口：

```python
def assemble_trace(
    run: AgentRun,
    events: tuple[RunEvent, ...],
    messages: tuple[RunMessage, ...],
    context_versions: tuple[ContextVersion, ...],
) -> RunTrace: ...

class TraceService:
    async def get_trace(self, run_id: str) -> RunTrace: ...

class JsonTraceExporter:
    def export(
        self,
        trace: RunTrace,
        output_path: str | Path,
        *,
        include_content: bool = False,
        allow_partial: bool = False,
    ) -> Path: ...
```

- `TraceService` 只调用 `RunRepository.find_run()`、`load_events()`、`load_messages()`、`load_context_versions()`；找不到 Run 抛出明确 `LookupError`，不返回伪 Trace。
- `assemble_trace()` 是纯函数；不读文件、不写文件、不调用 Runtime。
- `export()` 是唯一写文件动作；终态完整 Trace 可直接导出，部分 Trace 必须显式 `allow_partial=True`，同一路径允许覆盖。

## 3. 数据模型与组装规则

`models.py` 使用不可变 dataclass 与固定 `StrEnum`，不引入 Pydantic、子类 Span 或开放注册表：

```text
SpanKind: RUN | LLM | TOOL | APPROVAL | DELEGATION
TraceSpanStatus: COMPLETED | FAILED | CANCELLED | WAITING | INCOMPLETE
TraceIssue: kind, event_sequence?, message_id?, span_id?, evidence
TraceSpan: span_id, kind, parent_span_id, started_at, ended_at, status,
           start_event_sequence, end_event_sequence, message_ids,
           context_version, attributes
TraceMetrics: llm_duration_ms, tool_duration_ms, approval_wait_ms,
              longest_tool_duration_ms, failed_tool_count, incomplete_span_count,
              critical_path_ms
RunTrace: schema_version, run, source metadata, spans, messages,
          context_versions, metrics, issues
```

- `RunTrace` 直接保存既有 `RunMessage[]`、`ContextVersion[]`；Span 只保存 message ID / context version 引用。
- `attributes` 只能写入当前已知事实：LLM 的 model_id/call_index，Tool 的 call_id/tool_name/status，Approval 的 approval_id/approved，Delegation 的 child_run_id/task_id/target_agent_id/status；禁止放消息正文、工具完整输出或新事件副本。
- 至少定义 `MISSING_EVENT_PAIR`、`MISSING_MESSAGE`、`MISSING_CONTEXT_VERSION`、`UNSUPPORTED_EVENT`、`CONFLICTING_REFERENCE` 五类 Issue。它们是结构化重建证据，不是 PR7 根因。
- `record_hash` 对 `run.to_dict()`、按 sequence 的 `event.to_dict()`、按 sequence 的 `message.to_dict()`、按 version 的 ContextVersion 序列做稳定 JSON 序列化并 SHA-256；不包含 `assembled_at`。

组装步骤：

1. 创建唯一 RUN 根 Span，起点取 Run 开始时间或首事件时间；
2. 按 sequence 扫描 Event，维护未闭合 LLM、Tool、Approval、Delegation 关联；
3. LLM 依据当前严格串行约束，以 `LLM_STARTED` / `LLM_COMPLETED` 配对，并把 `context_version` 和响应 message ID 关联到 Span；
4. Tool 以 `call_id` 配对 Started/Completed，工具参数来自 source response message，结果来自 result message；
5. Approval 以 `approval_id` 关联 WAITING / RESOLVED，并以 `call_id` 关联对应 Tool；
6. Delegation 以 child_run_id 关联 REQUESTED / SUBMITTED / COMPLETED；
7. 每次关联失败追加 Issue，不删除原事件；未闭合 Span 标记 INCOMPLETE；
8. Run 非终态、存在未闭合 Span 或关键关联缺失时设置 `is_partial=True`，最后计算 Metrics。

## 4. 一致性、导出与安全

- PR1 已拒绝 JSON / 字段 / sequence / run_id 损坏；PR2 只将语义不完整转为 Issue。
- 非终态 Trace 表示读取时的连续事件前缀，不承诺跨文件快照。
- JSON 默认导出 schema、来源元数据、Span、Issue、message ID、ContextVersion 引用和脱敏摘要；不导出 Prompt、模型正文、工具输出或 Secret。
- `include_content=True` 才导出完整内容；无论内容模式如何，`record_hash` 都指向原始权威事实。
- Trace 不写回 Runtime，JSON 文件不是查询或恢复来源。

## 5. 测试与验收

新增 Golden fixtures 覆盖：纯 LLM、Tool 成功/失败、审批通过/拒绝、Delegation、运行中 Run、历史审批字段缺失、消息缺失、ContextVersion 缺失、未配对事件。

必须验证：

1. 相同权威输入得到相同 record_hash、Span 语义和规范化 Golden JSON；
2. 所有五类 Span 的配对与 parent RUN 关系正确；
3. 语义缺失生成 Issue / INCOMPLETE，不抛读取异常；
4. 结构合法但无 Event 的 Run 返回只有 RUN Span 的部分 Trace；
5. Service 不写文件，Exporter 不修改 Runtime；
6. 默认导出不含正文，显式内容导出和部分 Trace 导出需显式开关；
7. `compileall src`、`pytest -q tests/trace tests/runtime_v2`、`git diff --check` 通过。

## 6. 推荐提交顺序与完成门槛

1. **Trace 模型与纯组装骨架**：模型、RUN/LLM/Tool 配对、Golden 最小用例；
2. **完整关联与 Issue**：Approval、Delegation、ContextVersion、部分 Trace、Metrics；
3. **Service 与 Exporter**：仓储加载、显式 JSON、安全内容模式；
4. **回归与 Wiki**：补齐 Golden、Runtime 回归、增量 Wiki。

完成后存在 `TraceService.get_trace(run_id)` 和显式 JSON 导出；不存在 EvalRunner、Dataset、OTLP 或 Runtime 自动 Trace。
