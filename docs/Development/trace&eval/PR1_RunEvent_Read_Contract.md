# dotClaw PR1 开发文档：运行事件读取契约

## 0. 文档信息

| 项目 | 内容 |
|---|---|
| 文档状态 | 已确认开发计划 |
| 所属阶段 | 阶段一：Trace 基础闭环 |
| PR 编号 | PR1 |
| PR 名称 | 运行事件读取契约 |
| 目标分支 | 以实际开发分支为准 |
| 适用代码 | dotClaw Runtime v4 |
| 核心产出 | `RunEvent` 可反序列化，`RunRepository` 可读取完整事件序列 |

---

## 1. PR 定位

### 1.1 唯一目标

补齐 Runtime 运行事件的读取闭环，使后续 PR2 能够从仓储读取：

```text
AgentRun
RunEvent[]
RunMessage[]
ContextVersion[]
```

PR1 完成后，只要求支持：

```text
events.jsonl
→ RunEvent[]
```

PR1 不负责生成 `RunTrace`，也不负责解释事件之间的语义关系。

### 1.2 当前问题

当前 Runtime 已经能够：

- 将 `RunEvent` 追加到 `events.jsonl`；
- 校验事件 sequence 连续；
- 校验事件引用的 RunMessage 已经持久化；
- 在内存仓储中保存事件。

但当前缺少：

- 面向 `events.jsonl` 的严格读取与反序列化契约；
- `RunRepository.load_events()`；
- 文件仓储的事件读取实现；
- 内存仓储的事件读取实现。

文件仓储已有私有 `_run_event_from_dict()`，仅供成功提交意图读取复用；它会将部分类型错误静默降级为默认值，且没有 `events.jsonl` 读取入口。因此，后续 Trace 模块无法通过 Runtime 的应用接口读取完整事件序列。

---

## 2. 最终边界

### 2.1 PR1 包含

PR1 只实现以下内容：

1. 文件仓储内严格的 `_run_event_from_dict()`；
2. `RunRepository.load_events()`；
3. `RunRepositoryAdapter.load_events()`；
4. `InMemoryRunRepository.load_events()`；
5. 补充审批事件必要的结构化字段；
6. 单元测试、仓储契约测试和 Runtime 回归测试；
7. 更新 Runtime Wiki 中的仓储能力说明。

### 2.2 PR1 不包含

PR1 明确不实现：

- `trace/` 顶层模块；
- `RunRecord`；
- `RunRecordReader`；
- `RunTrace`；
- `TraceSpan`；
- `TraceAssembler`；
- `TraceService`；
- `TraceQueryService`；
- Trace Metrics；
- JSON Trace 导出；
- OTLP；
- EvalCase；
- Playback；
- Re-execution；
- Regression Gate；
- Failure Attribution；
- `RunEventRecorderPort`；
- RuntimeEngine 事件记录重构；
- Journal 修改或迁移；
- `RunStatistics` 修复；
- `duration_ms` 计算；
- 新存储格式或数据迁移。

---

## 3. 修改文件

预计修改：

```text
src/dotclaw/runtime/
├── application/
│   ├── ports.py
│   └── engine.py
└── adapters/
    ├── run_repository.py
    └── in_memory_run_repository.py
```

预计修改或新增测试：

```text
tests/runtime_v2/
├── test_repositories.py
├── test_e4_runtime_safety.py
└── test_tool_executor_adapter.py
```

实际测试目录应遵循仓库当前结构，不为本 PR 单独调整测试目录体系。

---

## 4. 详细设计

### 4.1 文件仓储中的 `RunEvent` 反序列化

在：

```text
src/dotclaw/runtime/adapters/run_repository.py
```

修改既有私有函数：

```python
def _run_event_from_dict(data: JSONMap) -> RunEvent:
    ...
```

它是文件存储格式到领域事实的 Adapter 内部转换，同时供 `events.jsonl` 与 `success_commit.json.completed_event` 读取复用；不迁入领域模型，也不作为公开 API。

#### 4.1.1 读取字段

需要读取：

```text
run_id
sequence
event_type
occurred_at
message_ids
summary
data
```

#### 4.1.2 必填字段

| 字段 | 要求 |
|---|---|
| `run_id` | 非空字符串 |
| `sequence` | 大于 0 的整数，布尔值不视为整数 |
| `event_type` | 合法的 `RunEventType` |
| `occurred_at` | 非空字符串；PR1 不解析或强制 ISO-8601 格式 |

#### 4.1.3 可选字段

历史事件允许缺少下列字段，并使用默认值：

| 字段 | 默认值 |
|---|---|
| `message_ids` | `()` |
| `summary` | `""` |
| `data` | `{}` |

#### 4.1.4 字段校验

`message_ids`：

- 必须是数组；
- 每个元素必须是字符串；
- 不在该函数中校验对应消息是否存在。

`data`：

- 必须是 JSON 对象；
- 不对不同 EventType 的业务字段进行强制校验。

可选字段仅在**缺失**时使用默认值；字段已出现但类型错误时必须失败，不能静默过滤或降级。例如 `message_ids` 含非字符串元素、`summary` 不是字符串或 `data` 不是对象，均视为损坏记录。

#### 4.1.5 错误行为

以下情况抛出 `ValueError`：

- `run_id` 缺失或为空；
- `sequence` 不是正整数；
- `event_type` 无效；
- `occurred_at` 缺失、为空或不是字符串；
- `message_ids` 不是字符串数组；
- `summary` 不是字符串；
- `data` 不是对象。

PR1 不新增专用异常类。

---

### 4.2 `RunRepository` 接口

在：

```text
src/dotclaw/runtime/application/ports.py
```

为 `RunRepository` 增加：

```python
async def load_events(
    self,
    session_id: str,
    run_id: str,
) -> tuple[RunEvent, ...]:
    """读取按 sequence 连续排列的运行事件。"""
```

该接口与以下读取方法同级：

```text
load_run()
load_messages()
load_context_versions()
```

不新增：

```text
EventRepository
TraceRepository
RunRecordRepository
```

`RunEvent` 继续由现有 `RunRepository` 管理。

---

### 4.3 文件仓储事件读取

在：

```text
src/dotclaw/runtime/adapters/run_repository.py
```

新增异步入口：

```python
async def load_events(
    self,
    session_id: str,
    run_id: str,
) -> tuple[RunEvent, ...]:
    return await asyncio.to_thread(
        self._load_events_sync,
        session_id,
        run_id,
    )
```

新增同步实现：

```python
def _load_events_sync(
    self,
    session_id: str,
    run_id: str,
) -> tuple[RunEvent, ...]:
    ...
```

#### 4.3.1 读取流程

```text
定位 events.jsonl
    ↓
文件不存在：返回 ()
    ↓
逐行读取
    ↓
JSON 解码
    ↓
require_json_map()
    ↓
_run_event_from_dict()
    ↓
校验 run_id
    ↓
校验 sequence
    ↓
返回 tuple[RunEvent, ...]
```

#### 4.3.2 sequence 规则

事件必须严格满足：

```text
1, 2, 3, ..., n
```

禁止：

- 从 0 开始；
- 从大于 1 的数字开始；
- sequence 跳号；
- sequence 重复；
- sequence 倒序。

虽然当前写入路径已经校验 sequence，读取路径仍必须独立校验，防止文件损坏或人工修改。

#### 4.3.3 run_id 规则

每一行事件必须满足：

```python
event.run_id == run_id
```

否则抛出 `ValueError`。

#### 4.3.4 空文件、空白行与运行中读取

- 文件不存在：返回空元组；
- 文件存在但内容为空：返回空元组；
- 中间存在空白行：视为文件损坏，抛出 `ValueError`；
- 文件末尾正常换行不视为空白事件行。
- 任一非空但无法解析的行（包括最后一行）均视为损坏，抛出 `ValueError`；不因其位于文件末尾而静默忽略。

`load_events()` 可以在 Run 仍运行时调用。它返回读取时已成功追加、且从 sequence 1 开始连续的事件前缀；不保证与 `run.json`、`messages.json` 或 `checkpoint.json` 构成跨文件事务快照。若读取恰好与追加写入交错而看到未完成行，调用方应在后续轮询重试，仓储不猜测该行是并发写入还是持久化损坏。

#### 4.3.5 错误信息

错误信息至少包含：

```text
events.jsonl
行号
失败原因
```

示例：

```text
events.jsonl 第 3 行解析失败：事件序号必须为 3，实际为 5
```

PR1 不新增统一存储错误类型。

#### 4.3.6 不做的校验

`load_events()` 不负责：

- 校验 `event.message_ids` 对应的 RunMessage 是否存在；
- 校验 `context_version` 是否存在；
- 配对 Started 和 Completed；
- 判断 LLM、Tool、Approval 或 Delegation 是否完整；
- 判断 RunStatus 与终态 Event 是否一致；
- 生成 TraceIssue；
- 自动修复事件文件。

这些属于 PR2 的 Trace 解释逻辑。

生产 Runtime 不维护独立的内存事件列表；运行中进度读取以已持久化的 `events.jsonl` 为权威来源。`InMemoryRunRepository` 的事件容器仅用于测试和隔离执行，不构成生产缓存或第二事实源。

---

### 4.4 内存仓储事件读取

当前 `InMemoryRunRepository` 已有：

```python
self._events: dict[
    tuple[str, str],
    tuple[RunEvent, ...],
]
```

只新增：

```python
async def load_events(
    self,
    session_id: str,
    run_id: str,
) -> tuple[RunEvent, ...]:
    return self._events.get((session_id, run_id), ())
```

不增加新的事件容器，不复制事件对象。

---

### 4.5 审批事件字段补充

PR1 只修改两处事件 data，使后续 PR2 可以稳定关联一次审批过程。

#### 4.5.1 `WAITING_APPROVAL`

当前等待审批事件需要补充：

```text
approval_id
call_id
```

目标写法：

```python
event_number = await self._event(
    run,
    event_number,
    RunEventType.WAITING_APPROVAL,
    (tool_message.message_id,),
    "等待工具审批",
    {
        "approval_id": record.approval_id,
        "call_id": tool_call.call_id,
    },
)
```

目的：后续 TraceAssembler 可以建立：

```text
Tool Call
→ Approval Request
→ Approval Resolution
```

而不依赖中文 summary。

#### 4.5.2 `APPROVAL_RESOLVED`

审批解决事件补充：

```text
approval_id
approved
```

目标写法：

```python
event_sequence = await self._event(
    run,
    checkpoint.event_sequence,
    RunEventType.APPROVAL_RESOLVED,
    (),
    "审批已通过" if approved else "审批已拒绝",
    {
        "approval_id": approval_id,
        "approved": approved,
    },
)
```

兼容要求：历史事件即使缺少这些字段，`_run_event_from_dict()` 仍必须能够读取。

PR1 不对历史文件做迁移。

---

## 5. 不修改的事件

### 5.1 Tool 事件

当前 Tool 事件已经包含：

```text
call_id
tool_name
status
source_response_message_id
result_message_id
error_summary
```

PR1 不修改 Tool Event。

### 5.2 Delegation 事件

当前 Delegation Event 已包含：

```text
child_run_id
task_id
target_agent_id
target_session_id
status
```

PR1 不修改 Delegation Event。

### 5.3 LLM 事件

当前 `LLM_STARTED` 已包含：

```text
call_index
model_id
context_version
incremental_message_ids
context_hash
tool_schema_hash
```

PR1 不修改 `LLM_STARTED`。

PR1 也不为 `LLM_COMPLETED` 新增 `call_index`。

理由：

- 当前同一 Run 内 LLM 调用严格串行；
- PR2 可以先根据事件顺序进行配对；
- 在实际组装中确认现有数据不足后，再增加必要关联字段；
- PR1 不提前扩大 Engine 修改范围。

---

## 6. 统计数据边界

PR1 不处理统计功能，但需要明确后续数据归属。

### 6.1 `run.json`

继续保存少量 Run 级权威摘要：

```text
llm_call_count
tool_call_count
tokens_in
tokens_out
duration_ms
resume_count
```

这些数据用于不构建 Trace 时快速查看一次 Run 的基本资源消耗。

### 6.2 `RunEvent` 和 `RunMessage`

保存过程证据：

```text
单次 LLM 模型和 Token
Tool 开始与结束
Tool 状态
审批结果
Delegation 结果
```

### 6.3 `RunTrace`

后续 PR2 保存可重新计算的派生指标：

```text
LLM 总耗时
Tool 总耗时
审批等待时长
失败 Tool 数量
未完成 Span 数量
```

### 6.4 PR1 约束

PR1 不修改：

- `RunStatistics`；
- `_with_llm_statistics()`；
- `_with_tool_statistic()`；
- `duration_ms` 计算；
- Token 统计字段；
- Journal 统计逻辑。

这些应作为后续独立问题处理，不能与事件读取混合。

---

## 7. 测试计划

### 7.1 文件仓储 Event 反序列化测试

必须覆盖：

1. `RunEvent.to_dict()` 后可以由 `_run_event_from_dict()` 正确恢复；
2. `message_ids` 缺失时使用空元组；
3. `summary` 缺失时使用空字符串；
4. `data` 缺失时使用空对象；
5. 非法 `event_type` 失败；
6. `sequence=0` 失败；
7. `sequence` 为布尔值时失败；
8. 空 `run_id` 失败；
9. 非字符串 `occurred_at` 失败；
10. 空字符串 `occurred_at` 失败；
11. `message_ids` 包含非字符串元素时失败；
12. `data` 不是对象时失败。
13. 已出现但非字符串的 `summary` 失败。

### 7.2 文件仓储 `load_events()` 测试

必须覆盖：

1. `events.jsonl` 不存在时返回 `()`；
2. 空文件返回 `()`；
3. 一个事件正常读取；
4. 多个事件按顺序读取；
5. JSON 损坏时失败；
6. 根节点不是对象时失败；
7. sequence 不从 1 开始时失败；
8. sequence 跳号时失败；
9. sequence 重复时失败；
10. event.run_id 不匹配时失败；
11. 中间空白行时失败；
12. 错误信息包含行号。
13. 最后一行 JSON 损坏时失败，不静默返回此前事件。
14. 运行中读取只返回连续事件前缀；该测试不要求跨文件一致快照。

### 7.3 内存仓储测试

必须覆盖：

1. 未写入事件时返回 `()`；
2. 追加后可以读取；
3. 多个事件保持追加顺序；
4. 不同 Run 的事件隔离。

### 7.4 审批事件测试

必须覆盖：

1. 等待审批时事件包含 `approval_id`；
2. 等待审批时事件包含 `call_id`；
3. 审批通过时包含 `approved=true`；
4. 审批拒绝时包含 `approved=false`；
5. `APPROVAL_RESOLVED` 包含正确 `approval_id`；
6. 历史无 data 的审批事件仍可读取。

### 7.5 Runtime 回归测试

至少执行：

- 普通无工具执行；
- 单工具执行；
- 多工具执行；
- 审批等待；
- 审批通过恢复；
- 审批拒绝；
- 中断重试；
- 取消；
- SuccessCommit 恢复；
- 文件仓储现有测试；
- 内存仓储契约测试。

---

## 8. 实施步骤

建议按四个提交完成。

### Commit 1：Event Deserialization

修改：

```text
runtime/adapters/run_repository.py
```

完成：

- 严格化既有 `_run_event_from_dict()`；
- 对应单元测试。

### Commit 2：Repository Read Contract

修改：

```text
runtime/application/ports.py
runtime/adapters/run_repository.py
runtime/adapters/in_memory_run_repository.py
```

完成：

- `RunRepository.load_events()`；
- 文件仓储读取；
- 内存仓储读取；
- sequence 和 run_id 校验；
- 仓储测试。

### Commit 3：Approval Event Data

修改：

```text
runtime/application/engine.py
```

只补充：

- `WAITING_APPROVAL.data`；
- `APPROVAL_RESOLVED.data`。

不进行 Engine 重构。

### Commit 4：Regression Tests and Wiki

完成：

- Runtime 回归测试；
- Runtime Wiki 更新；
- 在 Wiki 中说明 `RunRepository` 已支持读取 Event；
- 明确 TraceAssembler 尚未实现。

---

## 9. 验收标准

PR1 合并前必须同时满足：

1. `RunEvent` 可以从 JSON 对象反序列化；
2. `RunRepository` 定义 `load_events()`；
3. 文件仓储实现 `load_events()`；
4. 内存仓储实现 `load_events()`；
5. 事件读取严格校验 sequence；
6. 事件读取严格校验 run_id；
7. 损坏 JSON 不被静默忽略；
8. 错误信息包含事件行号；
9. `WAITING_APPROVAL` 包含 `approval_id` 和 `call_id`；
10. `APPROVAL_RESOLVED` 包含 `approval_id` 和 `approved`；
11. 历史审批事件仍可读取；
12. 未创建 `trace/` 模块；
13. 未创建 `RunRecord` 或 `RunTrace`；
14. 未实现 TraceAssembler；
15. 未修改统计模型；
16. 未修改 Journal；
17. 未改变 Runtime 状态机流程；
18. 现有 Runtime 恢复测试全部通过。
19. 运行中读取以已追加的连续事件前缀为准，不承诺跨文件快照。
20. 不新增生产内存事件缓存或事件订阅机制。

---

## 10. PR 完成后的能力

PR1 完成后应支持：

```python
run = await repository.find_run(run_id)

if run is not None:
    events = await repository.load_events(
        run.session_id,
        run.run_id,
    )
```

返回结果：

```python
tuple[RunEvent, ...]
```

PR1 完成后仍不支持：

```python
trace = await trace_service.get_trace(run_id)
```

也不支持：

```python
eval_result = await eval_runner.run(case)
```

这些分别属于后续 PR。

---

## 11. PR2 前置条件

PR2 可以直接依赖以下现有接口：

```text
find_run()
load_events()
load_messages()
load_context_versions()
```

PR2 再实现：

```text
run_id
→ TraceService
→ 加载 Run/Event/Message/ContextVersion
→ assemble_trace()
→ RunTrace
```

PR1 不为 PR2 提前创建额外中间对象。

---

## 12. 最终判断

PR1 的最终边界为：

> 为 Runtime 补齐 RunEvent 的严格读取契约，并补充审批事件的最小结构化关联字段。

本 PR 只解决“事件能否可靠读取”，不解决“事件如何被解释为 Trace”。
