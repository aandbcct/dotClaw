# Agent Tracer 代码审查报告

> 审查日期：2026-06-11
> 审查范围：Agent Tracer 会话跟踪模块（tracer.py + loop.py 集成 + config + 测试）
> 审查基准：`docs/Development/tracer/2026-06-11-agent-tracer-design.md` 设计文档 + `docs/Development/prompt/code-review-prompt.md` 审查标准

---

## 审查总览

Agent Tracer 实现了双层输出（实时 trace.jsonl + 终态 report.json），完整覆盖设计文档的 6 个步骤类型（session / loop / prompt_built / llm_call / llm_response / tool_exec），start/done 配对机制 + incomplete 标记逻辑正确。与 AgentLoop 的集成点精确——TTFT 计算（llm_call.start → 首个 chunk）、流式生成耗时（llm_response.start → 最后 chunk）、工具执行时长，全部在正确的时间点调用。no-op 模式（enable_tracer=False）设计干净，所有公开方法入口零开销。

测试覆盖全面（11 个测试：禁用模式 / 基础流程 / 多轮工具 / step_id 连续性 / 失败记录 / report 构建 / 不完整状态 / 错误信息），使用 tempfile 隔离保证测试独立性。

无 Critical 问题。发现 1 个 Warning 和 4 个 Minor 问题。

| 严重级别 | 数量 | 说明 |
|----------|------|------|
| Critical | 0 | — |
| Warning | 1 | 建议修复 |
| Minor | 4 | 可后续改进 |
| Info | 2 | 可选优化 |

---

## Warning — 建议修复

### W1. [loop.py:390] `build_report()` 异常会错误地覆盖会话成功状态为失败

**位置**：`src/dotclaw/agent/loop.py:388-390`

**问题描述**：
成功路径中 `build_report()` 在 try 块内调用：

```python
# loop.py:387-398
if self._tracer:
    self._tracer.end_session(success=True, final_response=final_response)
    self._tracer.build_report()  # ← 这里可能抛异常

return AgentResult(...)

except Exception as e:  # ← 被这里捕获
    ...
    if self._tracer:
        self._tracer.end_session(success=False, error=error_msg)  # 覆盖为失败
```

`build_report()` 方法内部的 `json.dump(report, f, ...)` 如果因磁盘满、权限变更等抛出 `OSError`，异常会传播到外层 `except Exception`，触发 `end_session(success=False)`，将一次成功的会话**错误地**标记为失败。

**影响**：虽然概率极低（磁盘满），但会污染 trace 数据的准确性。

**建议**：将 `build_report()` 用 try/except 包裹：

```python
if self._tracer:
    self._tracer.end_session(success=True, final_response=final_response)
    try:
        self._tracer.build_report()
    except Exception:
        pass  # report 构建失败不影响会话结果
```

---

## Minor — 建议改进

### M1. [tracer.py:426] step_id 2 位数字格式，超过 99 步后格式不一致

**位置**：`src/dotclaw/agent/tracer.py:426-427`

**问题描述**：
```python
def _next_step_id(self) -> str:
    sid = f"s_{self._step_counter:02d}"
```
使用 `:02d` 零填充到 2 位。当 `_step_counter` 达到 100 时，step_id 变为 `s_100`（3 位），与前面 `s_00..s_99` 的 2 位格式不一致，可能影响基于字符串排序的场景。

**风险**：极低。每轮至少产生 2 个 step（llm_call + llm_response），100 步意味着 50 轮 ReAct 循环，远超过 `max_iterations=10` 的限制。但作为防御性编程，应使用更宽的格式。

**建议**：改为 `f"s_{self._step_counter:03d}"` 或 `f"s_{self._step_counter}"`（无零填充）。

---

### M2. [tracer.py 多处] `error: str = None` 类型标注不兼容

**位置**：`src/dotclaw/agent/tracer.py:84, 176, 214, 259`

**问题描述**：
```python
def end_session(self, ..., error: str = None) -> None:
```
`error: str = None` 在严格类型检查（mypy --strict）下会报 incompatible type。参数实际语义是 `str | None`。

**建议**：统一改为 `error: str | None = None`（Python 3.10+ 原生支持，dotclaw needs `>=3.13`）。

---

### M3. [tracer.py:370-383] 未配对 start 事件统一放入 round 0，可能归属错误

**位置**：`src/dotclaw/agent/tracer.py:370-383`

**问题描述**：
`_build_report_from_events()` 中处理未配对（仅有 start 无 done）的事件时，将这些事件全部放入 `rounds_map.get(0)` 对应的轮次中，因为 pending 事件不携带 round 信息。

这在崩溃场景下（最后一轮未来得及记录 done）会将该轮的步骤错误地归属到 round 0。

**建议**：在 `_append()` 中为 start 事件添加 round 字段，或在 pending_steps 中同时记录 round 信息。当前方案已可接受（incomplete 事件极少），但建议添加代码注释说明这个简化处理的已知限制。

---

### M4. [tracer.py:88-89] 未调用 `start_session` 直接调用 `end_session` 时 `total_duration_ms` 计算错误

**位置**：`src/dotclaw/agent/tracer.py:89`

**问题描述**：
```python
total_dur = int((time.time() - self._session_start_ts) * 1000)
```
如果由于编程错误（非预期调用顺序），`end_session()` 在 `start_session()` 之前被调用，`self._session_start_ts` 值为 `0.0`（初始值），`total_duration_ms` 将是从 Unix epoch 到当前时间的毫秒数，得到一个巨大的无意义值。

**建议**：添加防卫检查：
```python
if self._session_start_ts == 0.0:
    return  # start_session 未调用，静默跳过
```

---

## Info — 可选优化

### I1. [tracer.py:442] `_append()` 每次打开文件

`_append()` 每条事件都执行 `open(self._trace_file, "a")` → `json.dump` → `f.write("\n")` → 隐式 close。对于多轮、多工具调用的会话（20+ events），这是 20+ 次文件打开/关闭操作。可考虑持有文件句柄并在 `end_session` 时关闭，但当前实现简洁且足够可靠——频繁 flush 保证了崩溃时 trace 数据的完整性（不会丢失已在 OS buffer 中的数据）。

### I2. [tracer.py] 缺少 trace 文件清理策略

设计文档明确将"trace 文件自动清理/保留策略"标记为 out of scope，但当前实现完全没有清理机制。`data/traces/{YYYY-MM-DD}/{request_id}/` 会随使用持续增长。建议在下一迭代中添加基于日期/数量的清理逻辑。

---

## 架构审查结论

### 符合设计文档 ✓

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 双层输出（trace.jsonl + report.json） | ✓ | 实时 append + 终态构建 |
| 输出路径：data/traces/{date}/{req_id}/ | ✓ | `_output_dir / today / req_id` |
| 6 种步骤类型完整覆盖 | ✓ | session / loop / prompt_built / llm_call / llm_response / tool_exec |
| start/done 配对机制 | ✓ | step_id + pending_steps 字典 |
| incomplete 状态标记 | ✓ | 未配对的 start 标记为 incomplete |
| state: start / success / failure | ✓ | 三种状态正确使用 |
| TTFT 计算（llm_call.duration_ms） | ✓ | loop_start → 第一个 chunk |
| 生成耗时（llm_response.duration_ms） | ✓ | 首 chunk → 末 chunk |
| no-op 模式（enable_tracer=False） | ✓ | 所有公开方法入口检查，零开销 |
| DebugConfig.enable_tracer | ✓ | config.yaml + settings.py + _raw_to_config |
| 步骤专属字段 | ✓ | model / args / result / finish_reason / usage / duration_ms |
| session.start_session → end_session | ✓ | start 写 user_message，end 写 final_response + total_duration_ms |

### 数据流验证

| 路径 | 状态 | 验证 |
|------|------|------|
| config.yaml enable_tracer → DebugConfig | ✓ | settings.py 行 197, 530 |
| main.py → AgentTracer(data_root) | ✓ | main.py 行 255 |
| AgentLoop.__init__(tracer=) → self._tracer | ✓ | loop.py 行 60, 74 |
| start_session → start_loop → prompt_built | ✓ | loop.py 行 102, 138, 160 |
| llm_call_start → (first chunk) → llm_call_done + llm_response_start | ✓ | loop.py 行 175, 188-189 |
| (last chunk) → llm_response_done | ✓ | loop.py 行 205-211 |
| tool_exec_start → (execute) → tool_exec_done | ✓ | loop.py 行 273, 296 |
| end_loop → (next round / end_session) | ✓ | loop.py 行 242, 340 |
| end_session → build_report | ✓ | loop.py 行 389-390, 410-411 |

---

## 测试覆盖评估

| 测试类 | 测试数 | 覆盖内容 | 评价 |
|--------|--------|----------|------|
| TestDisabled | 1 | 禁用模式下全部方法 no-op，无文件产生 | ✓ 充分 |
| TestTraceJSONL | 5 | 基础会话流 / 单轮无工具 / 多轮多工具 / step_id 连续性 / 失败记录 | ✓ 充分 |
| TestReportJSON | 5 | 基础 report / 多工具 report / 崩溃 incomplete / 多轮 report / 错误信息 | ✓ 充分 |

**总计**：11 tests，3 个测试类。亮点：
- `test_step_id_continuity` 验证了跨会话的计数器重置
- `test_incomplete_due_to_crash` 覆盖了崩溃场景（只有 start 没有 done）
- `test_report_with_tool_execs` 覆盖了同一轮多工具调用且包含失败工具的场景
- 全部使用 `tempfile.TemporaryDirectory` 隔离

未覆盖但低优先级的场景：
- `end_session` 在 `start_session` 之前调用的防御测试
- 超大 trace（1000+ events）的 `build_report` 性能测试
- `_build_report_from_events` 处理空事件列表

---

## 整体评价

Agent Tracer 工程质量优秀。实现精准对齐设计文档——6 种步骤类型、start/done 配对、incomplete 标记、TTFT 和生成耗时分离、双层输出——全部按设计实现。与 AgentLoop 的集成点选择准确（首个 chunk → llm_call_done + llm_response_start、最后 chunk → llm_response_done、执行前后 → tool_exec_start/done），时机计算正确。no-op 模式的零开销设计干净。

测试覆盖全面——11 个测试覆盖了双模式、多轮、多工具、失败、崩溃 incomplete、step_id 连续性等关键路径，使用 tempfile 保证隔离。`_build_report_from_events` 的配对逻辑和 incomplete 处理是测试覆盖最充分的部分。

唯一 Warning（`build_report` 异常覆盖成功状态）是防御性编程改进，正常使用不会触发。

**审查结论：通过，建议修复 W1 后合入主干。**
