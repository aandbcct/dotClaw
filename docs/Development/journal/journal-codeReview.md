# Journal 统一观测模块 — Code Review Report

> 审查日期：2026-06-12
> 审查范围：Journal 统一观测模块（journal/ 包 9 文件 + AgentLoop 集成 + 配置 + 测试）
> 审查基准：`docs/Development/journal/design.md` 设计文档 + `docs/Development/prompt/code-review-prompt.md` 审查标准
> 审查目标：确保本模块作为后续开发基础，不会再需要大规模重构

---

## 审查总览

Journal 模块成功将三个分散的观测模块（AgentLogger / AgentTracer / MetricsCollector）合并为统一的 `Journal` 入口。核心架构——17 种事件类型覆盖 5 个域、`_emit()` 事件总线、`finalize()` 批量输出——完全对齐设计文档。旧模块（`agent/logger.py` / `agent/tracer.py` / `metrics/` 目录）已全部删除，AgentLoop 中的观测代码从三行一事简化为一行 `journal.xxx()`。

然而，该模块作为"后续开发基础"的当前版本存在 **5 个 Critical 问题**需要优先修复，否则后续 Phase（如 Skill 增强、工具链路完善）将基于错误的观测数据继续开发，累积技术债。

| 严重级别 | 数量 | 说明 |
|----------|------|------|
| Critical | 5 | **必须修复**，否则后续开发基于错误基础 |
| Warning | 3 | 建议修复 |
| Minor | 5 | 可后续改进 |
| Info | 2 | 可选优化 |

---

## 修复记录（2026-06-12）

| 编号 | 严重度 | 状态 | 修复说明 |
|------|--------|------|---------|
| C1 | Critical | ✅ 已修复 | `_build_context()` 新增 journal 参数，创建时机提前至 context 之前；`context.journal` 不再为 None。文件：`loop.py:83, 290-334` |
| C2 | Critical | ✅ 已修复 | `tool_end` status 从硬编码 `"success"` 改为 `result.is_error` 动态判定 + `result.error_type`。文件：`loop.py:197-202` |
| C3 | Critical | ✅ 已修复 | `_emit()` 中集成 `trace_sink`（实时逐行追加）和 `console_sink`（ERROR 实时输出）。文件：`journal.py:61-82` |
| C4 | Critical | ✅ 已修复 | journal 调用移入模块内部：`LLMProxy.chat()` 调 `llm_call_start/end`，`ToolExecutor.execute()` 调 `tool_start/end`。AgentLoop 删除冗余调用。文件：`proxy.py:93-110`, `executor.py:43-116`, `loop.py:125-200` |
| C5 | Critical | ✅ 已修复 | `_build_report` 和 snapshot 构建异常从 `except Exception: pass` 改为 `self.error(...)` 记录。文件：`journal.py:366-388` |
| W1 | Warning | ✅ 已修复 | `input_tokens` 收集从 `LLM_CALL_START` 移到 `LLM_RESPONSE_END`（只有后者包含真实 token 数据）。文件：`snapshot.py:217-229` |
| W2 | Warning | ✅ 已修复 | 删除 `resp_dur` 死变量和 `first_chunk` 死变量（耗时由 Journal 内部计时器管理）。文件：`loop.py:122, 159` |
| W3 | Warning | ✅ 已修复 | `tool_executor=None` 分支补 `journal.tool_start/end` 事件。文件：`loop.py:209-216` |
| M1 | Minor | ✅ 已修复 | `prompt_built` 补全 `system_prompt_hash` 和 `skills_injected` 参数。文件：`loop.py:103-110` |
| M2 | Minor | ✅ 已修复 | `llm_response_end` status 根据 `loop_finish_reason` 动态判定（`"error"` / `"truncated"` / `"success"`）。文件：`loop.py:156-160` |
| M3 | Minor | ✅ 已修复 | `memory_retrieval` 拆除 0.5ms hack，改用 `memory_retrieval_start/end()` 配对计时。文件：`journal.py:291-304`, `loop.py:294-299` |
| M4 | Minor | ⏸️ 保留 | SkillsProvider 中 `skill_script_exec(status="success")` 语义正确——prompt 注入阶段只注册可用脚本，真实执行发生在工具层 |
| M5 | Minor | ⏸️ 保留 | `finalize()` 当前拆分已足够清晰，66 行在可维护范围内 |

---

## Critical — 必须修复

### C1. [loop.py:333 + context.py:72] `context.journal` 永远为 None → SkillsProvider 观测断链

**位置**：
- `src/dotclaw/agent/loop.py:333` — `journal=None` 硬编码
- `src/dotclaw/agent/prompt/providers.py:117-124` — 死代码段

**问题描述**：
`AgentLoop._build_context()` 中将 `skill_registry` 正确传入，但 `journal` 被硬编码为 `None`：

```python
# loop.py:333
return Ctx(
    ...
    skill_registry=self._skill_registry,  # ✓ 正确传入
    journal=None,                          # ✗ 永远是 None！
)
```

导致 `SkillsProvider.provide()` 中的观测代码（providers.py:117-124）永远不执行：

```python
journal = context.journal    # ← 永远是 None
if journal:                  # ← 永远是 False
    for meta in all_metas:
        journal.skill_trigger(...)          # 死代码
        journal.skill_body_loaded(...)      # 死代码
        journal.skill_script_exec(...)      # 死代码
```

**风险**：Skill 激活、加载、脚本执行的观测事件**完全丢失**。后续任何依赖 Skill 观测数据的 Phase（如 Skill 使用率分析、脚本执行耗时统计）将得到零数据，需要重构观测层。

**修复**：
```python
# loop.py:333 — 将 journal 传入 context
return Ctx(
    ...
    journal=self._journal,  # 从 AgentLoop 属性获取
)
```

同时在 AgentLoop.__init__ 中保存 journal 引用，或在 `_build_context()` 签名中添加 journal 参数。

---

### C2. [loop.py:207] `tool_end` status 永远硬编码为 "success" → 工具观测完全失真

**位置**：`src/dotclaw/agent/loop.py:204-208`

**问题描述**：
```python
journal.tool_start(tc.name)
result = await self._tool_executor.execute(...)
journal.tool_end(
    tc.name,
    result_len=len(result.output),
    status="success",   # ✗ 硬编码！无论 ToolResult.is_error 是 True 还是 False
)
```

审批拒绝（`APPROVAL_DENIED`）、执行超时（`TIMEOUT`）、工具未找到（`TOOL_NOT_FOUND`）、执行异常（`EXECUTION_ERROR`）等错误场景被观测层**完全忽略**。

**风险**：
- `report.json` 中工具成功率永远为 100%
- `snapshot.json` 中 `ToolCallMetrics.success_rate` 永远为 1.0
- A/B 对比时无法发现工具失败率变化
- 后续任何依赖工具观测数据的 Phase 将基于完全失真的数据

**修复**：
```python
journal.tool_end(
    tc.name,
    result_len=len(result.output),
    status="error" if result.is_error else "success",
    error_type=result.error_type if result.is_error else "",
)
```

---

### C3. [journal.py:61-68] sink 模块未集成 → 实时输出功能完全缺失

**位置**：
- `src/dotclaw/journal/journal.py:61-68` — `_emit()` 方法
- `src/dotclaw/journal/sinks/trace.py` — 独立函数，无调用者
- `src/dotclaw/journal/sinks/console.py` — 独立函数，无调用者

**问题描述**：
`_emit()` 的设计文档描述为"发射事件：追加到事件列表，触发各 sink"，但实际实现**仅追加到内存列表**，完全不调用 `console_sink()` 或 `trace_sink()`：

```python
def _emit(self, event_type, data=None):
    event = AgentEvent(timestamp=time.time(), event_type=event_type, data=data or {})
    self._events.append(event)   # ← 只追加到内存，不调任何 sink
```

实际的 trace.jsonl 写入发生在 `finalize()` 中手动打开文件逐行写入（第 330-337 行），完全绕过了 `trace_sink()` 函数。`console_sink()` 从未被调用，`journal.error()` 事件不会实时显示在控制台。

**风险**：
- 错误事件只在 `finalize()` 后才能查看，实时报警功能缺失
- 崩溃前的事件全部丢失（trace 只在 finalize 时批量写入）
- `sinks/` 子包是完全的死代码，给后续开发者造成混淆
- 设计文档中"trace_sink 实时追加"的核心设计目标未实现

**修复**：两种方案择一：
1. **方案 A（推荐）**：在 `_emit()` 中调用 sink，实现真正的实时输出
2. **方案 B**：删除 `sinks/` 子包，将文件 I/O 逻辑内联到 `finalize()`，更新设计文档

方案 A 实现示例：
```python
def _emit(self, event_type, data=None):
    event = AgentEvent(timestamp=time.time(), event_type=event_type, data=data or {})
    self._events.append(event)
    if self._config:
        if self._config.trace:
            trace_sink(event, self._config.trace_dir, self._request_id)
        if self._config.console and event_type == EventType.ERROR:
            console_sink(event)
```

---

### C4. [llm/proxy.py:78 + tools/executor.py:41] 下游模块 journal 参数是死参数

**位置**：
- `src/dotclaw/llm/proxy.py:78` — `LLMProxy.chat(journal=)` 接收但不使用
- `src/dotclaw/tools/executor.py:41` — `ToolExecutor.execute(journal=)` 接收但不使用

**问题描述**：
两个下游模块都接受 `journal` 参数但完全不调用任何 journal 方法：

```python
# llm/proxy.py
async def chat(self, ..., journal: "Any | None" = None):
    ...
    pass  # journal 由调用方（AgentLoop）负责发射 LLM 事件

# tools/executor.py  
async def execute(self, ..., journal: Any | None = None):
    ...  # 完全不使用 journal
```

设计文档 §8.2 明确要求 LLMProxy 内部调用 `journal.llm_call_start()` 和 `journal.llm_call_end()`，但当前所有 LLM 事件都由 AgentLoop 在外围发射。这意味着：
- LLMProxy 内部的重试逻辑（fallback 到备用模型）无法被观测
- ToolExecutor 内部的审批流程耗时无法被观测
- 从 LLMProxy 调用者视角看，journal 参数是可用的（有类型提示），但实际上它什么都不做

**风险**：后续开发者会期望通过 `LLMProxy.chat(journal=journal)` 自动获得 LLM 观测，但实际上 AgentLoop 是在外部手动发射——任何新的 LLM 调用者若不手动发射，就会丢失观测。

**修复**：
1. 在 `LLMProxy.chat()` 内部调用 `journal.llm_call_start()` / `journal.llm_call_end()`，AgentLoop 删除外部调用
2. 在 `ToolExecutor.execute()` 内部调用 `journal.tool_start()` / `journal.tool_end()`
3. 或者：删除这两个参数，明确文档说明 Journal 由调用方管理

---

### C5. [journal.py:351-352] `_build_report()` 异常静默吞没

**位置**：`src/dotclaw/journal/journal.py:351-352`

**问题描述**：
```python
try:
    report = _build_report(...)
    with open(report_path, "w") as f:
        json.dump(report, f, ...)
except Exception:
    pass   # ✗ 静默吞没！
```

任何 report 构建错误（如 `_build_report()` 中的 KeyError、磁盘满、权限错误）都被 `pass` 吞没，运维人员无法察觉 report.json 已损坏。

**风险**：report.json 静默损坏且无感知，复盘时发现数据缺失无法排查原因。

**修复**：
```python
try:
    report = _build_report(...)
    with open(report_path, "w") as f:
        json.dump(report, f, ...)
except Exception as e:
    self.error("ERROR", "journal.report", f"构建 report.json 失败: {e}")
```

---

## Warning — 建议修复

### W1. [snapshot.py:217-222] `LLM_CALL_START` 处理中提取 `input_tokens` 永远为 0

**位置**：`src/dotclaw/journal/snapshot.py:217-222`

**问题描述**：
`SnapshotBuilder.process()` 在 `LLM_CALL_START` 事件中试图提取 `data.get("input_tokens", 0)`，但 `journal.llm_call_start()` 只发射 `{"loop_idx", "model", "attempt"}`，不包含 `input_tokens`。该值**永远为 0**。

**影响**：`SnapshotBuilder` 的 `_input_tokens` 列表和 `_model_input_tokens` 字典始终为空，所有 per-model token 统计为 0。

**建议**：将 `input_tokens` 从 `LLM_CALL_START` case 移除，改为在 `LLM_RESPONSE_END` case 中收集（该事件确实包含 `output_tokens`）。或让 `llm_call_start()` 也发射 input_tokens（需在 prompt_built 时计算）。

---

### W2. [loop.py:159] `resp_dur` 死变量

**位置**：`src/dotclaw/agent/loop.py:159`

**问题描述**：
```python
resp_dur = time.time()   # 从未被使用
journal.llm_response_end(
    input_tokens=input_tokens,
    output_tokens=output_tokens,
    tps=0.0,
    status="success",
    stop_reason=loop_finish_reason,
)
```

`resp_dur` 被赋值后从未被使用（Journal 内部通过 `_timers["llm_response"]` 计算耗时），造成代码意图模糊。

**建议**：删除该变量，或添加注释说明耗时由 Journal 内部管理。

---

### W3. [loop.py:217-222] `tool_executor` 为 None 时无 journal 事件

**位置**：`src/dotclaw/agent/loop.py:217-222`

**问题描述**：
```python
if self._tool_executor:
    journal.tool_start(tc.name)
    result = await self._tool_executor.execute(...)
    journal.tool_end(tc.name, ...)
else:
    messages.append(Message(...))   # 无 journal 事件
```

当 `tool_executor` 为 None 时（开发/测试环境可能发生），工具调用失败但 journal 没有收到任何事件，导致 report 中的循环记录缺少工具步骤。

**建议**：在 else 分支也发射 journal 事件：
```python
else:
    journal.tool_start(tc.name)
    messages.append(Message(...))
    journal.tool_end(tc.name, result_len=0, status="error", error_type="no_executor")
```

---

## Minor — 建议改进

### M1. [loop.py:107-111] `prompt_built()` 缺少 `system_prompt_hash` 和 `skills_injected`

AgentLoop 调用 `journal.prompt_built()` 时使用默认值（空字符串和 None），失去了 prompt 溯源能力。`system_prompt_hash` 是 A/B 对比时的关键字段（判断两次运行是否使用相同 prompt）。

### M2. [loop.py:158-166] `llm_response_end()` status 硬编码 "success"

与 C2 类似，`llm_response_end()` 的 `status` 永远为 `"success"`，即使 LLM 因 `finish_reason="length"` 被截断。应改为根据 `loop_finish_reason` 动态判定。

### M3. [journal.py:276-288] `memory_retrieval()` 使用 0.5ms hack

```python
def memory_retrieval(self, query, hit_count):
    duration_ms = (time.time() - start_time + 0.5)  # 最小 0.5ms
```

0.5ms 的硬编码最小延迟不反映真实耗时。如果 MemoryManager 的检索是异步的（数千个向量），实际耗时可能远大于此。

### M4. [providers.py:122-124] SkillsProvider 硬编码 `skill_script_exec` status="success"

即使脚本实际执行失败，`SkillsProvider.provide()` 在 prompt 阶段就会无脑标记脚本为 success。但由于 C1 导致此代码段永远不会执行，目前无实际影响。

### M5. [journal.py:311-377] `finalize()` 函数过长（66 行）

`finalize()` 承担了 trace 写入、report 构建、snapshot 构建、事件清空四个职责。建议拆分为 `_write_trace()`、`_write_report()`、`_write_snapshot()` 三个私有方法。

---

## Info — 可选优化

### I1. [journal/journal.py:41] `JournalConfig` 与 `JournalSettingsConfig` 字段完全相同但有两次定义

`journal.py` 中的 `JournalConfig`（运行时）和 `settings.py` 中的 `JournalSettingsConfig`（配置层）有相同的 5 个字段，但定义在两个地方，且通过 `getattr` 手动转换。如果新增配置字段，需要在三处（JournalConfig、JournalSettingsConfig、转换代码）同步修改。建议只保留一个定义。

### I2. [tests/journal/test_journal.py] 测试覆盖关键缺口

当前测试覆盖了 Journal 核心 API（11 个测试类），但缺少：
- `_build_report()` 的输出验证
- `SnapshotBuilder` 的方法测试
- `finalize()` 文件写入行为的集成测试
- sink 模块的测试

---

## 架构审查结论

### 符合设计文档 ✓（部分）

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 一个入口（Journal API） | ✓ | 17 种具名方法，调用方只传业务事实 |
| 多路输出（console/trace/report/snapshot） | ✗ | sink 模块未集成，实时输出缺失 |
| 零侵入 API（loop_idx/timestamp/duration 内化） | ✓ | 参数内化对照表全部实现 |
| 并发安全（一个 run 一个 Journal） | ✓ | 局部变量创建，天然隔离 |
| 类型安全（具名方法 + 显式参数） | ✓ | IDE 自动补全可用 |
| 旧模块删除（logger/tracer/metrics） | ✓ | 三个模块全部删除 |
| AgentContext.journal 传递 | ✗ | 硬编码 None，下游断链 |
| LLMProxy 接收 journal | ✓~ | 参数存在但未消费 |
| ToolExecutor 接收 journal | ✓~ | 参数存在但未消费 |
| config.yaml journal 段 | ✓ | 5 字段与设计一致 |
| 17 种事件全部定义 | ✓ | EventType 17 常量 + AgentEvent frozen dataclass |
| Session/ReAct/LLM/Tool/Skill/Memory/Error 域覆盖 | ✓ | 全部 5 个域有对应事件 |
| SnapshotBuilder（52 指标） | ✓ | process() + build() + 5 个子构建器 |
| Storage（快照读写 + A/B diff） | ✓ | save_snapshot/load_snapshot/diff_snapshots |

### 作为"后续开发基础"的可靠性评估

| 维度 | 评分 | 说明 |
|------|------|------|
| **API 稳定性** | ★★★★★ | 具名方法 + frozen dataclass，API 不会因重构改变 |
| **数据完整性** | ★★☆☆☆ | C1+C2 导致 Skill 和工具观测数据完全失真 |
| **实时性** | ★★☆☆☆ | C3 导致实时输出缺失，崩溃前数据丢失 |
| **下游集成** | ★★☆☆☆ | C4 导致 LLM/Tool 执行器内上下文丢失 |
| **可观测性** | ★★☆☆☆ | C5 导致 report 损坏无感知 |
| **配置正确性** | ★★★★☆ | I1 有重复定义但功能正确 |
| **测试覆盖** | ★★★☆☆ | 核心 API 覆盖好，但缺少 report/snapshot/sink 集成测试 |

修复 5 个 Critical 后，综合评分有望从 ★★☆☆☆ 提升至 ★★★★☆。

---

## 测试覆盖评估

| 测试类 | 测试数 | 覆盖内容 | 评价 |
|--------|--------|----------|------|
| TestSessionStart | 2 | session_start 提取 AgentContext | ✓ |
| TestSessionEnd | 1 | session_end 发射事件 | ✓ |
| TestLoop | 4 | loop_start 自增/loop_end/empty_action | ✓ |
| TestToolCall | 3 | tool_start/end 计时内化/前置检查 | ✓ |
| TestLLMCall | 5 | prompt_built/四阶段/耗时计算 | ✓ |
| TestSkill | 3 | skill_trigger/body_loaded/script_exec | ✓ |
| TestMemory | 2 | memory_retrieval/write | ✓ |
| TestError | 1 | error 事件 | ✓ |
| TestEventImmutability | 2 | frozen/事件类型唯一性 | ✓ |
| TestConcurrencySafety | 1 | 两实例独立 | ✓ |
| TestFinalize | 1 | finalize 后清空列表 | ✓ |

**总计**：25 tests，11 个测试类。核心 API 覆盖良好，但缺少集成测试。

---

## 整体评价

Journal 模块从架构设计到核心实现都是优秀的——17 种事件全覆盖、参数内化精准、并发模型安全、旧代码彻底清理。六个 Phase 中，架构设计最接近"一次写对、终身不乱"的目标。

但当前实现存在 5 个 Critical 问题，导致模块**不具备作为后续开发基础的可靠性**：Skill 观测完全断链（C1）、工具故障完全不可见（C2）、实时输出缺失（C3）、下游集成虚假（C4）、报告损坏无感知（C5）。这些问题若不修复，后续 Phase 将基于错误的观测数据继续开发，累积的技术债需要在更大规模上偿还。

**审查结论：不通过。修复 C1-C5 后重新审查。**
