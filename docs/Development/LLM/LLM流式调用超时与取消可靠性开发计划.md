# LLM 流式调用超时与取消可靠性开发计划

> 状态：待确认后开发  
> 建议分支：从 `master` 新建独立功能分支  
> 建议 PR：`LLM 流式调用超时与取消可靠性`，目标分支为 `master`

## 1. 背景与问题

当前生产调用链为：

```text
Channel / CLI
  → SessionInteractionService
  → SessionRunCoordinator
  → RuntimeEngine
  → LLMPort
  → LLMProxyAdapter
  → LLMProxy
  → ModelRouter
  → OpenAICompatibleClient
  → OpenAI SDK AsyncStream / HTTP SSE
```

PR8 `[EXT]` 正式实验暴露了一个已有的生产链路缺口：当已建立的模型 SSE 流长期不返回首个 chunk 或后续长期空闲时，调用链可以无限等待。普通 CLI 对话能够完成，只能证明接口在该次单轮交互中可用，不能证明串行批量流式调用具备超时和取消语义。

当前代码事实：

- `OpenAICompatibleClient.chat()` 未传入请求级 HTTP 超时；流式迭代没有显式关闭 SDK stream。
- `LLMProxy.chat()` 在 `await anext(chat_iter)` 等待首个 chunk，首包等待没有独立时限。
- `LLMProxyAdapter.cancel()` 是空实现，无法中断正在进行的模型调用。
- `LLMProxy` 的重试次数来自路由配置；PR8 CLI 的 `--retry-count` 尚未传入实际代理调用条件。
- PR8 批量执行器仅在整批完成后写 JSONL 和快照，运行中无法从已落盘工件判断卡在模型、工具还是 Judge。

## 2. 目标、范围与非目标

### 2.1 目标

1. 为 OpenAI 兼容的真实模型调用落实可配置的连接、请求、首包和流空闲时限。
2. 使 Runtime 的取消请求能够关闭对应的在途流，并使等待中的 `complete()` 及时返回可分类错误。
3. 在代理层明确区分“首包失败、流空闲超时、主动取消、已输出后的流中断”，保持现有重试/降级边界。
4. 允许 Benchmark 显式传入实际生效的 timeout 与 retry 条件，快照记录的配置必须等于执行配置。
5. 形成可单 Case 复现、可自动验证的超时和取消测试，作为 PR8 `[EXT]` 重跑的前置条件。

### 2.2 范围

- `src/dotclaw/llm/`：客户端请求控制、流关闭、代理重试与可观测阶段。
- `src/dotclaw/runtime/adapters/llm_proxy_adapter.py`：按 `run_id` 管理在途调用并落实取消。
- `benchmarks/business_baseline.py`：仅接入已实现的公开调用控制；不在本 PR 引入 Benchmark 进度日志。
- 与上述行为对应的单元、契约和真实调用诊断测试。

### 2.3 非目标

- 不改变模型路由、工具/审批/委派、Runtime 状态机或 Session 持久化语义。
- 不实现断点续传、请求重放、跨进程恢复或新的通用任务调度器。
- 不把被取消或超时的请求伪装为成功，不保存 reasoning 正文或完整模型请求。
- 不在本 PR 落实 PR8 的样本级进度 JSONL；该能力留在 PR8 分支的 Benchmark 进度与失败诊断改动中。

## 3. 已确认契约

### 3.1 调用控制

新增不可变 `LLMCallControl（单次模型调用控制参数）`，由调用方在一次调用开始前冻结，至少包含：

- `connect_timeout_seconds`：建连与请求建立时限；
- `first_chunk_timeout_seconds`：从请求发出到首个标准化 `ChatChunk` 的时限；
- `idle_timeout_seconds`：首包后相邻有效流事件的最大间隔；
- `total_timeout_seconds`：整次调用的最大时长；
- `retry_count`：本次调用允许的额外重试次数。

默认值由 LLM 配置提供。普通 CLI 未显式传入时仍使用默认控制；Benchmark 显式传入时必须覆盖默认值，并写入快照。

### 3.2 失败与重试语义

| 阶段 | 结果 | Proxy 行为 |
|---|---|---|
| 建连/请求建立超时 | `CallSetupError` | 可按本次 `retry_count` 重试或切换候选模型 |
| 首包超时 | `CallSetupError` | 可重试或切换候选模型 |
| 首包前主动取消 | 取消错误 | 不重试、不降级，向 Runtime 终止该 Run |
| 已输出后流空闲超时 | `NonRetryableStreamError` | 不重试、不降级，保留“已部分输出后中断”边界 |
| 已输出后主动取消 | 取消错误 | 不重试、不降级 |
| SDK/网络异常 | 按是否已有可见输出分类 | 复用现有可见输出边界 |

取消、超时和关闭流必须幂等：重复调用不会抛出二次错误，也不会取消其他 `run_id` 的调用。

### 3.3 资源所有权与取消

- `OpenAICompatibleClient（OpenAI 兼容客户端）` 拥有一次 SDK stream 的创建和关闭；无论完成、异常、超时还是取消，均在 `finally` 中关闭该 stream。
- `LLMProxy（模型调用代理）` 拥有候选模型、重试和降级决策；它只消费统一的调用控制与分类异常，不保存 Runtime 状态。
- `LLMProxyAdapter（运行时到模型代理的适配器）` 拥有 `run_id → 在途调用` 的短生命周期映射；`cancel(run_id)` 只取消该映射中的任务并等待其资源释放。
- `RuntimeEngine` 保持既有的 `LLMPort.cancel(run_id)` 调用边界，不直接依赖 SDK 客户端。

## 4. 实施阶段

### 阶段一：冻结控制参数和错误契约

**目标**：先建立供应商无关的调用控制和失败分类，避免把 HTTP/SSE 细节泄漏到 Runtime。

**修改项**：

- 修改 `src/dotclaw/llm/base.py`：新增 `LLMCallControl（单次调用控制参数）` 与供应商无关的超时/取消异常类型。
- 修改 `src/dotclaw/config/settings.py`：增加 LLM 默认调用控制配置，并保证旧 YAML 缺字段时使用稳定默认值。
- 修改 `src/dotclaw/llm/proxy.py`：`chat()` 接收可选的调用控制，并以其中的 `retry_count` 覆盖全局默认重试数。

**测试与门槛**：

- 配置默认、显式覆盖、非法非正时限、负重试次数均有测试。
- 不传调用控制的现有调用方行为与当前配置一致。
- 代理测试证明 `retry_count=0` 不执行额外重试，且配置值不再只是报告元数据。

### 阶段二：实现 OpenAI 兼容请求与流控制

**目标**：将超时落实到 HTTP 请求、首包和流空闲三个可观察边界，并保证释放流资源。

**修改项**：

- 修改 `src/dotclaw/llm/openai_compat.py`：将控制参数传给 SDK 请求；为首包和每个后续 chunk 设置独立等待；在 `finally` 显式关闭 SDK stream。
- 对 SDK stream 的关闭操作建立兼容封装，仅依赖项目锁定 SDK 实际提供的异步关闭 API；关闭失败只作为原错误的附加上下文，不覆盖原始失败分类。
- 保持 `_StreamParseState（单次流解析状态）` 只属于当前请求，不能被超时或取消后的下一次调用复用。

**测试与门槛**：

- Fake SDK 分别模拟：建连阻塞、首包阻塞、首包后空闲、正常完成、取消期间关闭流异常。
- 断言每条路径都执行关闭，且正常完成只关闭自己的 stream 一次。
- 两个并发调用中取消其中一个，不得影响另一个的文本、工具调用或 finish/usage。

### 阶段三：代理重试与 Runtime 取消闭环

**目标**：让取消和超时跨越 Adapter、Proxy、Client 三层准确传播。

**修改项**：

- 修改 `src/dotclaw/runtime/adapters/llm_proxy_adapter.py`：登记当前 `run_id` 的调用任务；在 `complete()` 的 `finally` 清理登记；在 `cancel()` 中取消并等待对应任务结束。
- 修改 `src/dotclaw/llm/proxy.py`：在候选、重试和异步生成器退出路径关闭当前迭代器；取消不进入重试/降级逻辑。
- 只在错误尚未产生可见 reasoning/response 时允许重试或候选降级；已输出后超时保留为不可重试流中断。

**测试与门槛**：

- Runtime 测试覆盖：调用中取消、取消后 Run 终态、重复取消、不同 `run_id` 隔离。
- Proxy 测试覆盖：首包超时可重试、流空闲超时不可重试、取消不重试、重试次数精确。
- `rg -n "async def cancel" src/dotclaw/llm src/dotclaw/runtime/adapters` 的实现中不得保留空的 LLM 取消路径。

### 阶段四：PR8 EXT 条件接入与受控诊断

**目标**：让 PR8 记录的模型条件与实际执行一致，并在重跑前定位单 Case 真实链路。

**修改项**：

- 修改 `benchmarks/business_baseline.py`：把 `--timeout-seconds`、`--retry-count` 转为 `LLMCallControl` 并传入真实 LLM 调用与 Judge 调用。
- 删除仅以外层 `asyncio.wait_for` 包裹整个 `LLMPort.complete()` 的临时路径，避免取消清理被外层等待掩盖。
- 增加受控诊断入口或测试夹具：固定一个 EXT Case、一次采样、无基线写入，输出脱敏阶段时间点和最终分类。

**测试与门槛**：

- 注入 Fake LLM/Judge，断言 Benchmark 快照中的 timeout/retry 与实际接收的控制参数一致。
- 单 Case 真实诊断只在显式命令下运行；成功、首包超时、取消、流空闲超时均能区分。
- 该阶段不启动 180 条正式 EXT 采样。

### 阶段五：文档、回归与正式实验前门槛

**目标**：以可复跑证据确认生产修复与 Benchmark 接入完成。

**修改项**：

- 更新 LLM 开发文档，记录调用控制、取消边界、默认配置与不支持的恢复语义。
- 在 PR8 README 中仅补充 EXT 正式实验的前置条件与诊断命令，不写任何新的正式结果数字。

**验收门槛**：

- `tests/llm`、`tests/runtime_v2`、`tests/benchmarks` 的相关测试通过。
- 全量 `pytest`、`compileall`、`git diff --check` 通过。
- 单 Case 真实诊断完成并生成可审阅的脱敏阶段结果；只有此后才允许重启 EXT 180 条正式实验。

## 5. 文件清单与兼容策略

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/dotclaw/llm/base.py` | 修改 | 增加供应商无关的调用控制和错误契约 |
| `src/dotclaw/config/settings.py` | 修改 | 加载默认 timeout/retry 配置，旧 YAML 保持默认兼容 |
| `src/dotclaw/llm/openai_compat.py` | 修改 | 请求/首包/空闲控制与 stream 关闭 |
| `src/dotclaw/llm/proxy.py` | 修改 | 按调用控制重试、取消和生成器退出 |
| `src/dotclaw/runtime/adapters/llm_proxy_adapter.py` | 修改 | 以 `run_id` 隔离在途调用和取消 |
| `benchmarks/business_baseline.py` | 修改 | 将 EXT CLI 条件传至真实调用 |
| `tests/llm/`、`tests/runtime_v2/`、`tests/benchmarks/` | 修改/新增 | 契约、并发取消、真实诊断入口测试 |
| `docs/Development/LLM/` | 新增/修改 | 调用控制与运行边界文档 |

本任务不涉及数据迁移。旧配置文件没有调用控制字段时使用默认值；不保留新的并行旧接口。只有全部调用方迁移、相关测试通过且搜索确认无引用后，才删除 PR8 中临时的外层超时包装。

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| SDK 取消 API 与预期不同 | 先用项目锁定版本编写 SDK 契约测试，再封装关闭调用 |
| 已输出内容后错误重试导致重复交付 | 保持既有“已可见输出后不可重试”规则 |
| 取消清理阻塞调用方 | 将关闭流与任务清理置于受限时的 finally 路径，并测试重复取消 |
| Benchmark 参数与实际调用不一致 | 控制参数由同一对象传给代理并从同一对象序列化到快照 |
| 真实诊断产生费用或外部波动 | 仅显式执行单 Case 单次诊断；不将诊断结果作为正式基线 |

## 7. 推荐提交顺序

1. `定义LLM调用控制与超时错误契约`
2. `实现OpenAI流式请求超时与资源关闭`
3. `实现LLM代理取消与重试控制`
4. `接入PR8 EXT调用控制与单Case诊断`
5. `补齐LLM流可靠性文档与回归测试`

## 8. 最终验收清单

- [ ] 连接、首包、空闲、总时长和重试控制均有明确所有者与测试。
- [ ] 正常完成、异常、超时、取消均释放对应 SDK stream。
- [ ] 取消精确按 `run_id` 隔离，重复取消安全，不影响其他 Run。
- [ ] 已输出后的失败不重试；首包前失败按固定次数重试/降级。
- [ ] PR8 快照中的 timeout/retry 等于实际传入模型与 Judge 的条件。
- [ ] 单 Case 真实诊断能定位模型首包、流空闲、工具回合、Judge 四个阶段。
- [ ] 未完成或诊断运行绝不写入正式 EXT 基线。
- [ ] 相关专项、全量测试、编译和差异检查通过。
