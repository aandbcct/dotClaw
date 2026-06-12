# Journal —— 统一观测模块设计文档

> 状态：设计完成，待实现
> 日期：2026-06-11

## 1. 动机

### 1.1 现状问题

当前项目有三个独立的观测模块，各自维护、各自注入：

| 模块 | 位置 | 职责 | 输出 |
|------|------|------|------|
| `AgentLogger` | `agent/logger.py` | 日志基础设施 + 请求追踪摘要 | 控制台 + `data/dotclaw.log` |
| `AgentTracer` | `agent/tracer.py` | 步骤级微观追踪 | `data/traces/{date}/{req_id}/trace.jsonl` + `report.json` |
| `MetricsCollector` | `metrics/` 目录（6 文件） | 事件采集 + 量化指标 + A/B 对比 | `data/snapshots/{run_id}.json` |

**三个核心问题：**

1. **分散**：三个模块各自独立注入 AgentLoop，三个入口、三种调用方式、三种格式。同一件事（如工具调用）被记录了三次。

2. **Logger 扛双责**：`AgentLogger` 同时承担"全局日志基础设施"和"请求追踪摘要"两个完全不同的职责。其中 `TraceRecord` 和 `/debug` 命令用户从未消费，功能与 `AgentTracer` 的 `report.json` 高度重叠。

3. **冗余调用**：AgentLoop 主流程中充斥 `tracer.xxx()` / `logger.xxx()` / `collector.xxx()` 三行一事的冗余代码，干扰主流程可读性。

### 1.2 用户消费行为

实际复盘时用户只看两种产出：

- **trace.jsonl + report.json**（复盘单次请求链路）
- **snapshots/*.json**（A/B 对比多次运行指标变化）

控制台纯文本日志仅在报错时作为"报警器"使用——看到 ERROR 后去 JSON 里复查。

### 1.3 目标

将三个模块合并为一个统一的 **Journal** 模块，实现：

- **一个入口**：所有观测事件只发射一次
- **多路输出**：事件按需路由到控制台 / trace / report / snapshot
- **零侵入 API**：调用方只描述业务事实，时序和上下文全部内化
- **并发安全**：一次 `run()` 一个 Journal 实例，天然隔离
- **类型安全**：具名方法 + 显式参数，IDE 自动补全

---

## 2. 架构概述

```
AgentLoop / LLM代理 / 工具执行器 / 记忆管理 / 提供商
                    │
                    │  journal.xxx()  ← 唯一入口
                    ▼
            ┌──────────────┐
            │   Journal     │  事件总线（内存事件列表）
            └──────┬───────┘
                   │  事件按类型分流
     ┌─────────────┼─────────────┬──────────────┐
     ▼             ▼             ▼              ▼
  console_sink  trace_sink  report_builder  snapshot_builder
    （实时）      （实时）     （finalize）     （finalize）
     │             │             │              │
     ▼             ▼             ▼              ▼
   控制台      trace.jsonl   report.json   snapshots/*.json
（仅ERROR/     （逐行追加）   （结构化叙述）  （量化指标）
 WARNING）
```

### 2.1 与标准 logging 的关系

Journal 和 Python `logging` 模块**平行共存、互不干扰**：

```
┌── logging 管道（保留不动） ────────────────────────┐
│  logging.basicConfig() 在 main.py 直接调用           │
│  25+ 模块的 logger = logging.getLogger("dotclaw.xxx") │
│  logger.info/debug/error → 控制台 + data/dotclaw.log │
└────────────────────────────────────────────────────┘

┌── Journal 管道（新增） ────────────────────────────┐
│  journal.error() → console_sink → 控制台             │
│  journal.error() → trace_sink  → trace.jsonl         │
│  journal.tool_start() → trace_sink → trace.jsonl     │
│  journal.finalize() → report.json + snapshot.json    │
└──────────────────────────────────────────��─────────┘
```

- `AgentLogger` 类删除，其 `_setup_logging()` 逻辑迁到 `main.py` 直接调用
- 各模块的 `logger.info/error/debug()` 完全不受影响
- `journal.error()` 是观测管道内的错误记录，与 `logger.error()` 互不依赖

---

## 3. 事件类型

共 17 种标准化事件，覆盖 5 个域：

### 3.1 会话

| 事件 | 触发时机 | data |
|------|---------|------|
| `SESSION_START` | `journal.session_start(ctx)` | `config_hash` |
| `SESSION_END` | `journal.session_end(reason)` | `exit_reason` |

### 3.2 ReAct 循环

| 事件 | 触发时机 | data |
|------|---------|------|
| `LOOP_START` | `journal.loop_start()` | `loop_idx`（内部自增） |
| `LOOP_END` | `journal.loop_end(action)` | `loop_idx`, `action` |
| `EMPTY_ACTION` | `journal.empty_action()` | `loop_idx` |

### 3.3 LLM 调用（四阶段）

```
PROMPT_BUILT  →  LLM_CALL_START  →  LLM_CALL_END  →  LLM_RESPONSE_END
                                                │
                                         LLM_RESPONSE_START
                                           （同一时刻）
```

| 事件 | 触发时机 | data |
|------|---------|------|
| `PROMPT_BUILT` | `journal.prompt_built(msg_count, ctx_len, ...)` | `loop_idx`, `message_count`, `context_length`, `system_prompt_hash`, `skills_injected`, `tool_count` |
| `LLM_CALL_START` | `journal.llm_call_start()` | `loop_idx`, `model`, `attempt` |
| `LLM_CALL_END` + `LLM_RESPONSE_START` | `journal.llm_call_end()` | `loop_idx`, `model`, `duration_ms`；内部自动补射 RESPONSE_START |
| `LLM_RESPONSE_END` | `journal.llm_response_end(input_tokens, output_tokens, tps, status, stop_reason)` | `loop_idx`, `input_tokens`, `output_tokens`, `duration_ms`, `tps`, `status`, `stop_reason` |

### 3.4 工具调用

| 事件 | 触发时机 | data |
|------|---------|------|
| `TOOL_START` | `journal.tool_start(name)` | `loop_idx`, `tool_name`, `attempt` |
| `TOOL_END` | `journal.tool_end(name, result_len, status, error_type)` | `loop_idx`, `tool_name`, `duration_ms`（内部计算）, `result_len`, `status`, `error_type` |

### 3.5 Skill / 记忆 / 错误

| 事件 | 触发时机 | data |
|------|---------|------|
| `SKILL_TRIGGER` | `journal.skill_trigger(name)` | `loop_idx`, `skill_name` |
| `SKILL_BODY_LOADED` | `journal.skill_body_loaded(name, cached)` | `loop_idx`, `skill_name`, `duration_ms`（内部计算）, `cached` |
| `SKILL_SCRIPT_EXEC` | `journal.skill_script_exec(name, status)` | `loop_idx`, `skill_name`, `duration_ms`（内部计算）, `status` |
| `MEMORY_RETRIEVAL` | `journal.memory_retrieval(query, hit_count)` | `loop_idx`, `query`, `duration_ms`（内部计算）, `hit_count` |
| `MEMORY_WRITE` | `journal.memory_write(write_type, status)` | `loop_idx`, `write_type`, `status` |
| `ERROR` | `journal.error(level, source, message)` | `loop_idx`, `level`, `source`, `message` |

---

## 4. Journal API 设计

### 4.1 核心原则

- **调用方只描述业务事实，不传上下文**：`model`、`loop_idx`、`request_id` 从 `AgentContext` 提取或内部管理
- **调用方不传时间戳和耗时**：`journal.tool_start("read_file")` 后自动计时，`journal.tool_end(...)` 自动计算 duration
- **一个 run 一个 Journal**：在 `AgentLoop.run()` 内部创建为局部变量，天然并发安全
- **具名方法 + 类型签名**：IDE 自动补全，参数不丢

### 4.2 完整 API

```python
class Journal:
    """
    统一观测日志。

    一次 AgentLoop.run() 创建一个实例。
    所有事件通过具名方法发射，参数只传业务事实。
    loop_idx / model / timestamp / duration 全部内化。
    """

    def __init__(self) -> None:
        self._session_id: str | None = None
        self._request_id: str | None = None
        self._model: str = ""
        self._loop_idx: int = 0
        self._events: list[AgentEvent] = []
        self._timers: dict[str, float] = {}
        self._config: JournalConfig | None = None

    # ═══ 会话 ═══

    def session_start(self, ctx: AgentContext, config: JournalConfig) -> None:
        """
        开始会话。从 AgentContext 提取 session_id、request_id、model。
        config 控制各 sink 的启停和输出路径。
        """
        ...

    def session_end(self, exit_reason: str) -> None:
        """结束会话。exit_reason: "success" | "error" | "interrupted" """
        ...

    # ═══ ReAct 循环 ═══

    def loop_start(self) -> None:
        """开始新一轮循环。loop_idx 内部自增。"""
        ...

    def loop_end(self, action: str) -> None:
        """结束当前循环。action: "tool_call" | "response" | "empty" """
        ...

    def empty_action(self) -> None:
        """记录一次空转。"""
        ...

    # ═══ LLM 调用 ═══

    def prompt_built(self, message_count: int, context_length: int,
                     system_prompt_hash: str = "",
                     skills_injected: list[str] | None = None,
                     tool_count: int = 0) -> None:
        """提示词构建完成。记录上下文快照。"""
        ...

    def llm_call_start(self, attempt: int = 1) -> None:
        """发起 LLM 调用。model 已在 session_start 中设定。"""
        ...

    def llm_call_end(self) -> None:
        """
        LLM 调用结束 / 响应开始。
        内部计算 duration_ms，自动补射 LLM_RESPONSE_START 事件。
        """
        ...

    def llm_response_end(self, input_tokens: int, output_tokens: int,
                         tps: float, status: str,
                         stop_reason: str) -> None:
        """LLM 响应结束。内部计算从 RESPONSE_START 到现在的 duration_ms。"""
        ...

    # ═══ 工具调用 ═══

    def tool_start(self, tool_name: str, attempt: int = 1) -> None:
        """开始执行工具。内部记录时间戳。"""
        ...

    def tool_end(self, tool_name: str, result_len: int, status: str,
                 error_type: str = "") -> None:
        """工具执行结束。内部计算 duration_ms。"""
        ...

    # ═══ Skill / 记忆 ═══

    def skill_trigger(self, skill_name: str) -> None: ...
    def skill_body_loaded(self, skill_name: str, cached: bool = False) -> None: ...
    def skill_script_exec(self, skill_name: str, status: str) -> None: ...
    def memory_retrieval(self, query: str, hit_count: int) -> None: ...
    def memory_write(self, write_type: str, status: str) -> None: ...

    # ═══ 错误 ═══

    def error(self, level: str, source: str, message: str) -> None:
        """
        记录错误/警告。
        触发 console_sink（实时控制台输出）和 trace_sink（写入 trace.jsonl）。
        level: "ERROR" | "WARNING" | "INFO"
        """
        ...

    # ═══ 生命周期 ═══

    def finalize(self) -> None:
        """
        会话结束处理：
        1. build_report() → data/traces/{date}/{request_id}/report.json
        2. build_snapshot() → data/snapshots/{run_id}.json
        3. 清空事件列表
        """
        ...
```

### 4.3 参数内化对照

| 参数 | 内化方式 |
|------|---------|
| `session_id` | 从 `AgentContext` 提取 |
| `request_id` | 从 `AgentContext` 提取 |
| `model` | 从 `AgentContext` 提取，一次设定，全局复用 |
| `loop_idx` | 内部计数器，`loop_start()` 时自增 |
| `timestamp` | `_emit()` 内部调用 `time.time()` |
| `ttft` | `llm_call_end()` 内部根据 `llm_call_start` 时间戳计算 |
| `duration_ms`（工具/LLM/Skill/记忆） | start/end 配对，内部相减 |
| `config_hash` | `session_start` 时内部调用 `_build_config_hash()` 计算 |

### 4.4 调用方效果对比

```python
# ═══════════════════════════════════════════════════
# 改造前 — AgentLoop.run() — 18 行观测代码
# ═══════════════════════════════════════════════════
tracer.start_session()
logger.new_request()
collector.on_event(SESSION_START)

for idx in range(max_loops):
    tracer.start_loop(idx)
    collector.on_event(REACT_LOOP_START, data={"loop_idx": idx})
    ...
    logger.log_tool_call("read_file", {"path": "..."})
    tracer.tool_exec_start("read_file")
    collector.on_event(TOOL_CALL_START, data={})
    tracer.tool_exec_done("read_file", result_len=738)
    collector.on_event(TOOL_CALL_END, data={"duration_ms": 45})
    ...
    tracer.end_loop(idx)
    collector.on_event(REACT_LOOP_END)

tracer.end_session()
tracer.build_report()
collector.finalize(...)
logger.record(trace)

# ═══════════════════════════════════════════════════
# 改造后 — 6 行
# ═══════════════════════════════════════════════════
journal = Journal()
journal.session_start(context, config.journal)

for _ in range(max_loops):
    journal.loop_start()
    ...
    journal.tool_start("read_file")
    result = tool.execute()
    journal.tool_end("read_file", result_len=738, status="success")
    ...
    journal.loop_end("tool_call")

journal.session_end("success")
journal.finalize()
```

---

## 5. 输出端（Sinks）

### 5.1 console_sink

- **触发**：仅 `ERROR` 和 `WARNING` 事件
- **输出**：`stderr`，格式 `[LEVEL] source: message`
- **目标**：人眼快速感知异常，作为"报警器"

```python
def _console_sink(event: AgentEvent) -> None:
    if event.event_type == EventType.ERROR:
        data = event.data
        print(f"[{data['level']}] {data['source']}: {data['message']}", file=sys.stderr)
```

### 5.2 trace_sink

- **触发**：所有事件，实时逐行追加
- **输出**：`data/traces/{YYYY-MM-DD}/{request_id}/trace.jsonl`
- **格式**：每行一个 JSON 对象

```jsonl
{"ts": 1718100000.123, "type": "session.start", "data": {"config_hash": "a1b2c3d4"}}
{"ts": 1718100000.456, "type": "react.loop_start", "data": {"loop_idx": 1}}
{"ts": 1718100001.000, "type": "llm.call_start", "data": {"loop_idx": 1, "model": "deepseek-v4", "attempt": 1}}
{"ts": 1718100002.500, "type": "llm.call_end", "data": {"loop_idx": 1, "model": "deepseek-v4", "duration_ms": 1500}}
{"ts": 1718100002.500, "type": "llm.response_start", "data": {"loop_idx": 1}}
...
```

### 5.3 report_builder

- **触发**：`journal.finalize()`
- **输出**：`data/traces/{YYYY-MM-DD}/{request_id}/report.json`
- **逻辑**：将事件流按"发生了什么"重新组织为结构化叙述

```python
@dataclass
class Report:
    session_id: str
    request_id: str
    model: str
    duration_ms: int
    loop_count: int
    loops: list[LoopReport]       # 每轮循环的详情
    errors: list[ErrorEntry]      # 集中提取的错误
    tool_call_summary: dict       # 按工具名的调用统计
    memory_activity: dict         # 记忆检索/写入简述

def build_report(events: list[AgentEvent], session_id: str, ...) -> Report:
    # 将 SESSION_START → SESSION_END 之间的事件
    # 按 LOOP_START / LOOP_END 配对整理
    # LLM 和 Tool 事件按所属 loop_idx 归入对应的 LoopReport
    ...
```

### 5.4 snapshot_builder

- **触发**：`journal.finalize()`
- **输出**：`data/snapshots/{run_id}.json`
- **逻辑**：从事件流计算 52 个量化指标

与现有 `SnapshotBuilder` 功能完全一致，但输入从 `MetricsCollector` 的独立事件列表变为 Journal 的统一事件列表。五大指标类（`ReactLoopMetrics`、`ToolCallMetrics`、`SkillMetrics`、`MemoryMetrics`、`AgentGeneralMetrics`）保持不变。

---

## 6. 文件结构

```
src/dotclaw/journal/
├── __init__.py           # 导出 Journal, JournalConfig, EventType
├── journal.py            # Journal 类（事件总线 + API）
├── events.py             # AgentEvent + EventType（17 种事件定义）
├── sinks/
│   ├── __init__.py
│   ├── console.py        # console_sink：实时输出 ERROR/WARNING
│   └── trace.py          # trace_sink：实时追加 trace.jsonl
├── report.py             # build_report()：事件流 → Report
├── snapshot.py           # SnapshotBuilder（从 metrics/ 迁移，保持不变）
├── metrics_types.py      # 五大指标数据类（从 metrics/snapshot.py 拆分）
└── storage.py            # 快照读写 + A/B diff（从 metrics/ 迁移，保持不变）

tests/journal/
├── test_journal.py
├── test_events.py
├── test_sinks.py
├── test_report.py
├── test_snapshot.py
└── test_storage.py
```

### 6.1 旧文件处理

| 旧路径 | 处理方式 |
|--------|---------|
| `src/dotclaw/agent/logger.py` | 删除。`_setup_logging()` 迁到 `main.py`，`new_request()` 迁到 `loop.py` 的 `_build_context()` |
| `src/dotclaw/agent/tracer.py` | 删除。功能被 `sinks/trace.py` + `report.py` 替代 |
| `src/dotclaw/metrics/` 整个目录 | 迁移到 `journal/` 后删除 |
| `src/dotclaw/config/settings.py:DebugConfig` | 新增 `JournalConfig` 替代 |
| `config.yaml: debug` | 改为 `journal` 配置段 |
| `tests/test_phase5_acceptance.py:TestLoggerMerge` | 删除相关测试 |
| `tests/agent/test_tracer.py` | 重写为 `tests/journal/` 下的对应测试 |

### 6.2 配置变更

```yaml
# config.yaml —— 旧
debug:
  level: INFO
  log_file: ./data/dotclaw.log
  enable_tracer: true

# config.yaml —— 新
journal:
  log_level: INFO              # logging 级别（控制台 + log 文件）
  log_file: ./data/dotclaw.log # 传统 logging 文件
  trace_dir: ./data/traces     # trace.jsonl + report.json 目录
  snapshot_dir: ./data/snapshots # snapshot JSON 目录
  console: true                # console_sink 启停
  trace: true                  # trace_sink 启停
  snapshot: true               # snapshot 启停
```

---

## 7. 并发模型

### 7.1 设计

- Journal 实例在 `AgentLoop.run()` 内部创建为**局部变量**
- 一个 `run()` 一个 `Journal`，用完即弃
- 不存储在 `AgentLoop` 实例属性中
- 不需要锁、不需要 `reset()`、不需要连接池

```python
class AgentLoop:
    async def run(self, user_message: str) -> AgentResult:
        context = await self._build_context(user_message)
        journal = Journal()
        journal.session_start(context, self._config.journal)
        ...
        journal.finalize()
```

### 7.2 安全性证明

- `journal` 是 `run()` 的局部变量，两个并发的 `run()` 持有的是不同的 Python 对象
- `AgentContext` 是 `frozen=True` 不可变对象，Journal 只读不写
- `journal.finalize()` 写文件时，路径包含 `request_id`（每次唯一），不存在文件竞争
- `run()` 返回后 `journal` 超出作用域，GC 回收，内存释放

---

## 8. 与其他模块的交互

### 8.1 AgentLoop

Journal 在 `run()` 内部创建和使用。主循环中的 `loop_idx` 由 Journal 内部自增管理，不再需要 `for idx in range(...)` 中的显式索引变量。

### 8.2 LLMProxy

当前 LLMProxy 接收 `metrics_collector` 参数来发射 `LLM_REQUEST_START/END` 事件。改造后改为接收 `journal: Journal | None`：

```python
async def chat(self, messages, tools, model, purpose,
               stream, journal: Journal | None = None):
    if journal:
        journal.llm_call_start()
        journal.llm_call_end()
```

### 8.3 ToolExecutor

同理，`metrics_collector` 参数改为 `journal`：

```python
async def execute(self, name, arguments, channel,
                  journal: Journal | None = None) -> ToolResult:
```

### 8.4 MemoryManager

`context.metrics_collector` 改为 `context.journal`：

```python
def write(self, ...):
    if self._journal:
        self._journal.memory_write(write_type="daily_note", status="success")
```

### 8.5 SkillsProvider（DataProvider）

`context.metrics_collector` 改为 `context.journal`：

```python
def provide(self, context: AgentContext) -> str:
    if context.journal:
        context.journal.skill_trigger("code-review")
        context.journal.skill_body_loaded("code-review", cached=True)
```

### 8.6 AgentContext 变更

```python
# AgentContext —— 旧字段删除，新增字段
@dataclass(frozen=True)
class AgentContext:
    ...
    # 删除
    # metrics_collector: Any | None = None

    # 新增
    journal: Journal | None = None
```

---

## 9. 迁移步骤

按以下顺序执行，每步可独立验证：

### Phase 1：新建包体（不影响现有代码）
1. 创建 `src/dotclaw/journal/` 目录结构
2. 实现 `events.py`、`journal.py`、`sinks/`
3. 将 `metrics/` 下的 `snapshot.py`、`storage.py` 复制到 `journal/`，调整 import 路径
4. 实现 `report.py`
5. 新增 `JournalConfig` 数据类
6. 写单元测试

### Phase 2：改造 AgentLoop（核心变更）
1. 在 `AgentLoop.run()` 中创建 Journal 实例
2. 替换所有 `tracer.xxx()` / `collector.xxx()` / `logger.log_xxx()` 为 `journal.xxx()`
3. 删除 AgentLoop 构造函数中的 `tracer`、`metrics_collector`、`logger` 参数
4. AgentContext 中 `metrics_collector` 替换为 `journal`

### Phase 3：改造下游模块
1. `LLMProxy`: `metrics_collector` 参数改为 `journal`
2. `ToolExecutor`: 同上
3. `MemoryManager`: 消费 `context.journal`
4. `SkillsProvider`: 消费 `context.journal`
5. 散落在各模块的 `collector.on_event(X)` 调用全部替换

### Phase 4：迁移配置
1. `config.yaml` 新增 `journal` 段
2. `settings.py` 新增 `JournalConfig`，标记 `DebugConfig` 为 deprecated
3. `main.py` 直接调用 `logging.basicConfig()`，删除 `AgentLogger` 创建

### Phase 5：删除旧代码
1. 删除 `agent/logger.py`
2. 删除 `agent/tracer.py`
3. 删除 `metrics/` 整个目录
4. 清理 import 引用
5. 迁移或删除旧测试

### Phase 6：验证
1. 运行 `tests/journal/` 全部测试
2. 运行全量回归测试
3. 实际运行一次 Agent，检查 `data/traces/` 和 `data/snapshots/` 产出是否正常
