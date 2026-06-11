# Agent Tracer — 会话跟踪模块设计

**日期**：2026-06-11
**状态**：已确认，待实现

## 目标

在 Agent 每次 query 执行过程中，逐步骤记录状态转换，提供完整的可复盘链路。每一步记录开始时间、完成状态、失败原因。

## 双层输出

| 文件 | 写入时机 | 内容 |
|------|---------|------|
| `trace.jsonl` | **实时 incremental append** | 每一步状态转换事件（一行一条） |
| `report.json` | **会话结束后一次生成** | 将 start/done 配对合并，只保留终态 |

输出路径：`data/traces/{YYYY-MM-DD}/{request_id}/`

## trace.jsonl 格式

每条事件一行 JSON，通用骨架 + 步骤专属字段：

```json
// 通用骨架（所有事件共有）
{"ts":"2026-06-11T10:30:00.123+08:00","req_id":"r_abc","round":0,"step":"llm_call","state":"start","step_id":"s_03"}

// prompt_built（不区分 start/done，一步到位写 success，因为不会失败）
{"ts":"...","req_id":"r_abc","round":0,"step":"prompt_built","state":"success","step_id":"s_02","messages":[...],"msg_count":7,"est_tokens":2400}

// llm_call start + done
{"ts":"...","req_id":"r_abc","round":0,"step":"llm_call","state":"start","step_id":"s_03","model":"deepseek-v4"}
{"ts":"...","req_id":"r_abc","round":0,"step":"llm_call","state":"success","step_id":"s_03","usage":{"prompt_tokens":1500,"completion_tokens":300},"duration_ms":800}

// llm_response start + done
{"ts":"...","req_id":"r_abc","round":0,"step":"llm_response","state":"start","step_id":"s_04"}
{"ts":"...","req_id":"r_abc","round":0,"step":"llm_response","state":"success","step_id":"s_04","finish_reason":"tool_calls","usage":{"prompt_tokens":1500,"completion_tokens":300},"duration_ms":750}

// tool_exec start + done
{"ts":"...","req_id":"r_abc","round":0,"step":"tool_exec","state":"start","step_id":"s_05","tool_name":"read_file","args":{"path":"/tmp/x"}}
{"ts":"...","req_id":"r_abc","round":0,"step":"tool_exec","state":"success","step_id":"s_05","tool_name":"read_file","result":"file content..."}

// session start + done
{"ts":"...","req_id":"r_abc","round":-1,"step":"session","state":"start","step_id":"s_00","user_message":"帮我读文件"}
{"ts":"...","req_id":"r_abc","round":-1,"step":"session","state":"success","step_id":"s_00","final_response":"好的...","total_duration_ms":8500}

// loop start + done
{"ts":"...","req_id":"r_abc","round":0,"step":"loop","state":"start","step_id":"s_01","max_iterations":10}
{"ts":"...","req_id":"r_abc","round":0,"step":"loop","state":"success","step_id":"s_01"}
```

### llm_call vs llm_response 的生命周期

两个步骤触发点不同，时间差有诊断意义：

```
tracer.llm_call_start(model)              # 发起 API 请求前
    │
    ▼  async for chunk in self.llm.chat(...)
    │
    ├── 第一个 chunk 到达 ──→ tracer.llm_call_done(success)
    │                         tracer.llm_response_start()
    │
    ├── 后续 chunks ...
    │
    └── 最后一个 chunk ──→ tracer.llm_response_done(success, finish_reason, usage)
```

| 指标 | 含义 |
|------|------|
| `llm_call.duration_ms` | TTFT（首 token 延迟） |
| `llm_response.duration_ms` | 生成耗时（首 token → 末 token） |

如果 API 认证失败/超时等，连第一个 chunk 都没有 → 只写 `llm_call_done(failure)`，不写 `llm_response`。

### 步骤专属字段一览

| 步骤 | start 字段 | done 字段 |
|------|-----------|----------|
| `session` | `user_message` | `final_response`, `total_duration_ms` |
| `loop` | `max_iterations` | — |
| `prompt_built` | — | `messages`, `msg_count`, `est_tokens` |
| `llm_call` | `model` | `duration_ms`（TTFT） |
| `llm_response` | — | `finish_reason`, `usage`, `duration_ms`（生成耗时） |
| `tool_exec` | `tool_name`, `args` | `tool_name`, `result`, `duration_ms` |

### 字段说明

- `ts`：ISO 8601 带时区
- `req_id`：请求唯一标识（同 AgentContext.request_id）
- `round`：ReAct 循环轮次（session/loop 用 -1 表示跨轮次，loop 用所在轮次）
- `step`：步骤类型（`session`, `loop`, `prompt_built`, `llm_call`, `llm_response`, `tool_exec`）
- `state`：`start` / `success` / `failure`
- `step_id`：请求级自增序号（`s_00`, `s_01`, ...），用于 report 构建时配对 start/done
- `error`：仅 state=failure 时出现

## report.json 格式

```json
{
  "req_id": "r_abc",
  "user_message": "帮我读文件",
  "state": "success",
  "started_at": "2026-06-11T10:30:00.000+08:00",
  "total_duration_ms": 8500,
  "final_response": "好的，文件内容是...",
  "rounds": [
    {
      "round": 0,
      "state": "success",
      "prompt_built": {
        "state": "success",
        "started_at": "...",
        "msg_count": 7,
        "est_tokens": 2400
      },
      "llm_call": {
        "state": "success",
        "started_at": "...",
        "model": "deepseek-v4",
        "usage": {"prompt_tokens": 1500, "completion_tokens": 300},
        "duration_ms": 800
      },
      "llm_response": {
        "state": "success",
        "started_at": "...",
        "finish_reason": "tool_calls",
        "duration_ms": 750
      },
      "tool_execs": [
        {
          "state": "success",
          "started_at": "...",
          "tool_name": "read_file",
          "args": {"path": "/tmp/x"},
          "result": "file content...",
          "duration_ms": 150
        }
      ]
    }
  ]
}
```

### 构建规则

- 按 `step_id` 配对 start/done
- 同一轮同一步骤多次出现（如多个 tool_exec），按 `step_id` 顺序收集为数组
- 某个 `step_id` 只有 start、没有 done → state 标记为 `"incomplete"`
- 本轮有任何步骤 failure 或 incomplete → 本轮 `state` 为对应终态
- `session` 的 state 从 trace 中 `step="session"` 的最后一条获取

## Tracer API

```python
class AgentTracer:
    """会话跟踪器。追踪 trace.jsonl + 生成 report.json"""

    def __init__(self, config: DebugConfig, data_root: str)

    def start_session(self, req_id: str, user_message: str) -> None
    def end_session(self, success: bool, final_response: str = "", error: str = None) -> None

    def start_loop(self, round_num: int) -> None
    def end_loop(self, round_num: int) -> None

    def prompt_built(self, messages: list, msg_count: int, est_tokens: int) -> None

    def llm_call_start(self, model: str) -> str      # 返回 step_id
    def llm_call_done(self, step_id: str, success: bool, usage: dict = None, error: str = None, duration_ms: float = 0) -> None

    def llm_response_start(self) -> str
    def llm_response_done(self, step_id: str, success: bool, finish_reason: str = "", usage: dict = None, error: str = None, duration_ms: float = 0) -> None

    def tool_exec_start(self, tool_name: str, args: dict) -> str
    def tool_exec_done(self, step_id: str, success: bool, tool_name: str, result: str = "", error: str = None, duration_ms: float = 0) -> None

    def build_report(self) -> str                     # 返回 report 文件路径
```

## API 使用示例（AgentLoop.run 中的调用点）

```python
tracer.start_session(req_id=request_id, user_message=user_message)

for i in range(max_iterations):
    tracer.start_loop(round_num=i)

    # prompt_built 不会失败，一步到位
    messages = self._build_messages(user_message, context)
    tracer.prompt_built(messages=messages, msg_count=len(messages), est_tokens=estimate_tokens(messages))

    # llm_call：发起请求前
    sid_call = tracer.llm_call_start(model=context.model)
    loop_start = time.time()

    try:
        async for chunk in self.llm.chat(...):
            if first_chunk:
                # 第一个 chunk 到达 → llm_call 成功，llm_response 开始
                tracer.llm_call_done(sid_call, success=True,
                    duration_ms=(time.time() - loop_start) * 1000)
                sid_resp = tracer.llm_response_start()
                resp_start = time.time()
                first_chunk = False
            ...

        # 流结束 → llm_response 成功
        tracer.llm_response_done(sid_resp, success=True,
            finish_reason=finish_reason, usage=usage,
            duration_ms=(time.time() - resp_start) * 1000)

    except Exception as e:
        # 如果 llm_response 已开始 → 标记失败
        if sid_resp:
            tracer.llm_response_done(sid_resp, success=False, error=str(e))
        # 如果连第一个 chunk 都没到 → llm_call 也标记失败
        if not first_chunk:
            tracer.llm_call_done(sid_call, success=False, error=str(e))
        tracer.end_loop(round_num=i)
        tracer.end_session(success=False, error=str(e))
        raise

    # tool_exec
    for tc in tool_calls_pending:
        sid_tool = tracer.tool_exec_start(tool_name=tc.name, args=args)
        try:
            result = await self._tool_executor.execute(...)
            tracer.tool_exec_done(sid_tool, success=True,
                tool_name=tc.name, result=result.output[:500])
        except Exception as e:
            tracer.tool_exec_done(sid_tool, success=False,
                tool_name=tc.name, error=str(e))

    tracer.end_loop(round_num=i)

tracer.end_session(success=True, final_response=final_response)
tracer.build_report()
```

## 配置

```yaml
# config.yaml
debug:
  level: INFO
  log_file: ./data/dotclaw.log
  enable_tracer: false  # 新增：全局开关
```

```python
# config/settings.py — DebugConfig 新增字段
@dataclass
class DebugConfig:
    level: str = "INFO"
    log_file: str = "./data/dotclaw.log"
    enable_tracer: bool = False
```

### no-op 模式

`enable_tracer=False` 时，所有公开方法在入口处判断并直接返回，零开销：

```python
class AgentTracer:
    def __init__(self, config: DebugConfig, data_root: str):
        self._enabled = config.enable_tracer
        if not self._enabled:
            return

    def start_session(self, req_id: str, user_message: str) -> None:
        if not self._enabled:
            return
        # ...
```

## 实现计划

| # | 任务 | 文件 |
|---|------|------|
| 1 | DebugConfig 加 `enable_tracer: bool` | `config/settings.py` |
| 2 | config.yaml 加 `enable_tracer: false` | `config.yaml` |
| 3 | 新建 `AgentTracer` 类 | `src/dotclaw/agent/tracer.py`（新建） |
| 4 | AgentLoop 集成 tracer | `src/dotclaw/agent/loop.py` |
| 5 | main.py 创建 tracer 传入 AgentLoop | `src/dotclaw/main.py` |
| 6 | 单元测试 | `tests/test_agent_tracer.py`（新建） |

## 不在本次范围

- `prompt_built` 中 `messages` 的内存占用控制（全量保存可能很大）
- trace 文件自动清理/保留策略
- `/trace` CLI 命令（查看最近 trace）
