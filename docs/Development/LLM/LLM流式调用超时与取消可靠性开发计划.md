# LLM 流式调用超时与取消可靠性修复计划

> 状态：已完成（2026-08-09）
>
> 建议分支：从 `master` 新建独立修复分支
> 建议 PR：`修复 LLM 流式调用超时与取消可靠性`

## 1. 修复目标

修复真实 LLM 流在请求建立、首包等待、后续流空闲及 Runtime 取消时可能无限等待或不能释放连接的问题。修复后的生产 LLM 链路必须能承接业务调用方传入的 `timeout_seconds` 与 `retry_count`，使这些值实际进入代理和客户端。

本 PR 只修复 LLM 与 Runtime 适配层，不修改 `benchmarks/`、PR8 CLI、采样循环、快照、报告或业务实验命令。PR8 在其自身分支中仅负责把已有的实验参数传入本 PR 提供的调用接口。

## 2. 已确认问题

- `OpenAICompatibleClient（OpenAI 兼容客户端）` 未为单次请求落实连接、读取、写入和连接池超时；流式迭代结束时未显式关闭 SDK stream。
- `LLMProxy（模型调用代理）` 等待首个 chunk 与后续 chunk 时没有分别受限，因而首包卡住或首包后的 SSE 空闲都可能无限等待。
- `LLMProxyAdapter（运行时到模型代理的适配器）` 的 `cancel(run_id)` 未取消实际在途调用。
- 上层传入的 `timeout_seconds`、`retry_count` 尚未完整传入 `LLMProxy` 与 `OpenAICompatibleClient` 的实际调用路径。

## 3. 最小实现范围

### 3.1 调用条件透传

- 复用现有调用参数和配置入口，不新增全局调用控制对象、不调整模型路由策略。
- 将每次调用的 `timeout_seconds` 与 `retry_count` 从上层入口连续传至 `LLMProxy`、候选模型调用和 `OpenAICompatibleClient`。
- `retry_count` 只决定该次调用的额外重试次数；现有无参数调用继续使用当前默认值。

### 3.2 客户端超时与资源释放

- 在 `openai_compat.py` 为一次 SDK/HTTP 请求设置 connect、read、write、pool timeout；数值来源于该次调用的 `timeout_seconds`，不额外引入新的配置面。
- 对首个有效 chunk 使用首包等待时限；对首包后的每次下一项迭代使用流空闲时限。两者均基于本次 `timeout_seconds` 派生，具体比例作为实现常量集中定义并测试。
- 无论正常结束、异常、超时或取消，均在流式迭代的 `finally` 中显式关闭 SDK stream；关闭失败不得覆盖原始异常。

### 3.3 取消闭环

- `LLMProxyAdapter` 按 `run_id` 保存当前调用任务，并在 `complete()` 退出时清理。
- `cancel(run_id)` 仅取消该任务；取消会中断正在等待 HTTP/SSE 流的协程，并触发客户端的 `finally` 关闭 stream。
- 取消不进入重试或候选模型降级；重复取消和取消不存在的 `run_id` 安全返回。

## 4. 实施顺序

1. 追踪现有 `timeout_seconds`、`retry_count` 的入口与缺失转发点，补齐 `LLMProxy` 到客户端的最小参数链路。
2. 在 `OpenAICompatibleClient` 落实请求级 timeout、首包/流空闲等待和 stream 的 `finally` 关闭。
3. 在 `LLMProxyAdapter` 落实 `run_id` 任务登记、取消与 finally 清理；确保取消不重试。
4. 补齐针对阻塞与取消的 Fake SDK 测试，并执行相关回归。

## 5. 修改文件

| 文件 | 动作 | 目的 |
|---|---|---|
| `src/dotclaw/llm/openai_compat.py` | 修改 | 请求与流式 timeout、SDK stream 关闭、调用条件接收 |
| `src/dotclaw/llm/proxy.py` | 修改 | 透传 timeout/retry，并保持取消不重试 |
| `src/dotclaw/runtime/adapters/llm_proxy_adapter.py` | 修改 | 在途调用登记与按 `run_id` 取消 |
| `tests/llm/` | 修改/新增 | timeout、stream 关闭、重试次数的单元测试 |
| `tests/runtime_v2/` | 修改/新增 | 取消在途 LLM 调用及 `run_id` 隔离测试 |
| `docs/Development/LLM/` | 修改 | 记录修复边界和验证命令 |

只有在现有类型签名无法承载上述两个参数时，才对其作兼容性可选参数扩展；不新增独立配置模块、诊断命令或通用错误体系。

## 6. 验收标准

- [ ] 指定的 `timeout_seconds` 与 `retry_count` 均可从上层调用实际到达代理与客户端；无参数调用保持原有默认行为。
- [ ] 请求建立、首包等待和首包后流空闲都在有界时间内结束。
- [ ] 正常完成、请求异常、超时和取消均显式关闭对应 SDK stream，且关闭异常不掩盖原错误。
- [ ] `cancel(run_id)` 能终止正在等待的 HTTP/SSE 流，不影响其他 `run_id`，且不触发重试/降级。
- [ ] 相关 `tests/llm` 与 `tests/runtime_v2` 通过，随后执行全量 `pytest`、`compileall` 与 `git diff --check`。

## 7. 不在本 PR 验收的内容

- 不运行真实 LLM 业务质量实验或任何正式采样。
- 不生成、修改或校验业务基线、JSONL、快照、报告和简历量化数据。
- 不处理 PR8 的实验进度、失败诊断或业务结果写入；这些由 PR8 的后续变更独立验证。

## 8. 已实现边界与开发验证

- `timeout_seconds`、`retry_count` 作为兼容性可选参数扩展至 LLM 客户端（模型客户端），由 `LLMProxy`（模型调用代理）在显式传入时透传；未传入时仍使用 Provider 既有总尝试次数。显式 `retry_count` 表示额外重试次数。
- `OpenAICompatibleClient`（OpenAI 兼容客户端）以 60 秒的稳定默认请求预算创建 `httpx.Timeout`，连接、读取、写入与连接池共享该预算；首包和后续流空闲各使用调用预算的 50%。
- SDK stream 在正常完成、异常、超时和协程取消时均通过 `finally` 尽力关闭；关闭异常只记录，不覆盖原始错误。
- `LLMProxyAdapter`（运行时到模型代理的适配器）按 `run_id` 登记当前任务，`cancel(run_id)` 只取消该任务；取消会直达客户端关闭路径，不参与重试或候选降级。

开发回归命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\llm tests\runtime_v2 -q
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
```
