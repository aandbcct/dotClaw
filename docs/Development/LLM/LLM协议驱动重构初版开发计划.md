# LLM 协议驱动重构初版开发计划

> 状态：讨论初版（2026-08-12）
>
> 文档定位：记录当前已确认的本次开发边界，作为后续详细设计讨论的唯一方案基线。本文件不是最终技术设计，开发前按讨论结果原地修订。

## 1. PR 定位

### 1.1 唯一目标

本 PR 将 LLM 客户端选择从“供应商名称绑定实现”重构为“provider 管理供应商配置、driver 决定通信协议”，并让当前所有 OpenAI Chat Completions 兼容供应商复用同一个通用协议客户端。

### 1.2 当前问题

- `ProviderConfig`（供应商配置）目前只保存 API Key、Base URL、限流、重试和熔断参数，没有声明通信协议。
- `ModelRouter`（模型路由器）按 provider 名称查找客户端实现；未知 provider 会静默回退到 `QwenClient`（千问客户端）。
- jojocode 已存在于配置中并被 active 模型引用，但没有注册同名客户端，因此当前真实行为是实例化 `QwenClient`（千问客户端）。
- `QwenClient`（千问客户端）、`DeepSeekClient`（DeepSeek 客户端）和 `OpenAIClient`（OpenAI 客户端）只重复保存 API Key、Base URL 和 model，再调用相同的 OpenAI SDK Chat Completions 接口，没有独立协议职责。
- 静默回退会掩盖 provider 拼写、driver 拼写、端点协议不兼容和遗漏注册等配置错误。
- `load_router_config()` 当前没有把 YAML 中的 `circuit_breaker` 写入 `ProviderConfig`（供应商配置），导致配置值没有进入启动组装。

### 1.3 完成后的链路

```text
model
→ provider（供应商身份、账户、端点、限流、重试、熔断）
→ effective driver（模型覆盖或 provider 默认）
→ OpenAIChatCompletionsClient（OpenAI Chat Completions 协议客户端）
→ ChatChunk（标准化聊天增量包）
→ 现有 Runtime
```

Runtime 继续只消费现有 `ChatChunk`（标准化聊天增量包），不识别 provider、driver 或供应商 SDK 对象。

## 2. 已确认决策

1. provider 表示供应商账户与运维边界，拥有 API Key、Base URL、限流、重试和熔断配置。
2. driver 表示通信协议，不再按 qwen、deepseek、openai、jojocode 分别创建空壳客户端。
3. `ProviderConfig`（供应商配置）增加必填 driver，作为该 provider 的默认协议。
4. `ModelConfig`（模型配置）增加可选 driver 覆盖；有效协议为 `model.driver or provider.driver`。同一 provider 下不同模型可以显式选择不同协议，但仍共享 provider 级限流和熔断状态。
5. 本 PR 只实现并使用 `openai_chat_completions` driver。
6. 本 PR 不迁移 Responses API，不处理 reasoning item、response output item、`previous_response_id` 或供应商服务端 conversation。
7. 未知、缺失或未注册的 driver 在启动组装阶段失败，不允许默认 driver 和静默回退。
8. 当前 CLI 的 reasoning/response 双通道、工具调用、usage、timeout、取消、流关闭和模型降级语义必须保持不变。
9. 新路径完成并满足删除门槛后，物理删除旧供应商客户端、旧 provider 注册表、自动发现机制和 Qwen 回退逻辑。
10. 本 PR 不修改 Runtime 或 Tool 模块的公开契约和持久化格式。

## 3. PR 边界

### 3.1 包含内容

1. 为 provider 增加默认 driver，为 model 增加可选 driver 覆盖，并解析 effective driver。
2. 将现有 OpenAI Chat Completions 通用实现改造成可直接实例化的供应商无关客户端。
3. 在启动阶段完整校验 provider、model、driver 和 purpose 引用，删除未知 provider 到 Qwen 的回退。
4. 迁移 qwen、deepseek、openai、jojocode 配置，使它们复用同一个 Chat Completions driver。
5. 修复 `circuit_breaker` 配置加载，并保留 provider 级限流、重试和熔断语义。
6. 删除旧供应商客户端、旧注册表、自动发现机制及其测试和文档引用。

### 3.2 明确不包含

- 不实现或调用 Responses API。
- 不处理 reasoning item、encrypted reasoning content、response id 或跨轮供应商协议状态。
- 不使用 `previous_response_id`、conversation、store 或供应商服务端持久会话。
- 不修改 `LLMPort`（LLM 端口）、`RunMessage`（运行消息事实）、Conversation、Checkpoint 或 Context 数据格式。
- 不修改 Runtime 工具调用、审批、恢复或 Tool 执行协议。
- 不引入 OpenAI 托管 web search、file search、code interpreter、MCP 或其他内建工具。
- 不实现 Anthropic Messages、Gemini 原生协议或供应商专属高级参数。
- 不为未来 Responses API 预先建立 output item 数据模型、持久化字段或无当前调用方的抽象。
- 不运行 Benchmark 正式采样，不生成业务质量或可靠性基线结论。

## 4. 模块与文件结构

### 4.1 新增文件

```text
src/dotclaw/llm/drivers/
├── __init__.py
└── openai_chat_completions.py

tests/llm/
├── test_driver_config.py
└── test_driver_migration.py
```

`drivers/__init__.py` 提供显式 driver 名称到客户端构造函数的映射。它不扫描文件系统，不依赖模块导入副作用，也不为未来协议提前创建复杂插件框架。

### 4.2 修改文件

| 文件 | 修改目的 |
|---|---|
| `src/dotclaw/config/settings.py` | 解析 provider/model driver，修复 circuit breaker 加载，转换旧配置 |
| `src/dotclaw/llm/model_router.py` | 解析 effective driver、实例化协议客户端、删除 Qwen 回退 |
| `src/dotclaw/llm/proxy.py` | 保持路由、重试、可见输出和取消语义，更新客户端命名与类型引用 |
| `src/dotclaw/bootstrap/_host_components.py` | 在返回 LLM 组件前执行启动期配置校验 |
| `model_router_config.yaml` | 为当前 provider 显式声明 `openai_chat_completions` |
| `model_router_config.example.yaml` | 提供不含密钥的 driver 配置示例 |
| `tests/llm/`、`tests/runtime_v2/` | 迁移旧客户端测试并验证 Runtime 行为不变 |
| `docs/wiki/LLM 模块总体说明.md` | 实现稳定后增量更新当前架构事实 |

### 4.3 原则上不修改

```text
src/dotclaw/runtime/
src/dotclaw/tools/
src/dotclaw/channel/
src/dotclaw/llm/base.py
src/dotclaw/llm/reasoning.py
```

允许因 import 路径或测试替身名称变化做最小引用更新，但不得改变这些模块的领域行为或公开契约。

### 4.4 计划删除

```text
src/dotclaw/llm/providers/__init__.py
src/dotclaw/llm/providers/qwen.py
src/dotclaw/llm/providers/deepseek.py
src/dotclaw/llm/providers/openai.py
src/dotclaw/llm/openai_compat.py
```

`openai_compat.py` 的有效能力先迁入 `drivers/openai_chat_completions.py`，通过等价回归后再删除，不允许复制后长期保留两份实现。

## 5. 配置与接口设计

### 5.1 配置模型

```python
@dataclass
class ProviderConfig:
    driver: str
    api_key: str
    base_url: str
    rate_limit: dict
    circuit_breaker: dict
    retry: ProviderRetryConfig

@dataclass
class ModelConfig:
    provider: str
    driver: str | None
    model_id: str
    context_window: int
    tokenizer_encoding: str
    capabilities: list[str]
    status: str
    reasoning: ModelReasoningConfig
```

以上类型沿用现有配置模型，只增加协议选择字段，不新增持久化实体。

### 5.2 配置示例

```yaml
providers:
  qwen:
    driver: openai_chat_completions
    api_key: ${QWEN_API_KEY}
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1

  deepseek:
    driver: openai_chat_completions
    api_key: ${DEEPSEEK_API_KEY}
    base_url: https://api.deepseek.com

  jojocode:
    driver: openai_chat_completions
    api_key: ${JOJOCODE_API_KEY}
    base_url: <经当前服务分组确认的对话 Base URL>

models:
  gpt-5.6-luna:
    provider: jojocode
    model_id: gpt-3.6-luna
```

model 未声明 driver 时继承 provider 默认值。只有同一 provider 下的特定模型确实需要不同协议时才写 model.driver。

### 5.3 通用协议客户端

`OpenAIChatCompletionsClient`（OpenAI Chat Completions 协议客户端）直接接收供应商配置和模型配置：

```python
class OpenAIChatCompletionsClient(LLMClient):
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        policy: ReasoningPolicy | None = None,
    ) -> None:
        ...
```

它负责：

- 通过 `AsyncOpenAI.chat.completions.create()` 发起聊天请求；
- 转换普通消息、assistant 工具调用和 tool 结果；
- 解析 content、`reasoning_content`、工具调用增量、finish reason 和 usage；
- 保持现有 none/native/tags reasoning policy；
- 保持请求级局部流状态，并发调用互不污染；
- 保持请求、首包和流空闲 timeout；
- 在正常、异常、超时和取消路径关闭 SDK stream；
- embedding 继续通过 `AsyncOpenAI.embeddings.create()` 完成。

它不负责：

- 根据供应商名称改变协议；
- 读取 YAML；
- 管理限流、重试和熔断；
- 执行工具或修改 Runtime 状态；
- 在协议不匹配时切换到其他客户端。

### 5.4 driver 解析

`ModelRouter`（模型路由器）按以下顺序创建客户端：

```text
读取 model
→ 读取 model.provider
→ 解析 model.driver or provider.driver
→ 从显式 driver 映射取得构造函数
→ 注入 provider 连接配置与 model 配置
→ 缓存到当前 model_name
```

当前唯一合法 driver 是 `openai_chat_completions`。未知名称必须抛出明确配置错误，错误中包含 provider、model 和 driver 路径。

### 5.5 启动期校验

在 `_build_llm()` 完成配置加载后、构造可被 Runtime 使用的代理前校验：

1. defaults 引用的模型存在且 active；
2. 每个 active 模型引用的 provider 存在；
3. effective driver 非空且已注册；
4. driver 支持模型声明的 chat、function_calling 或 embedding 能力；
5. active provider 的 Base URL 合法；
6. active provider 的必要凭据不是空值或未展开的环境变量占位符；
7. purpose 中的 active 候选模型均存在且可解析；
8. 同一 provider 的限流、重试和熔断配置可以正常构建。

配置错误一次性报告明确字段路径，不延迟到对应模型首次请求。

## 6. 行为与一致性边界

### 6.1 Runtime 不变语义

- `RuntimeEngine`（运行时引擎）仍通过现有 `LLMPort.complete()` 发起模型调用。
- `LLMProxyAdapter`（Runtime 到模型代理适配器）仍接收 `ChatChunk`（标准化聊天增量包）。
- reasoning 只产生展示事件，不进入最终消息、Conversation 或 Context。
- response 继续聚合为最终 LLM 消息。
- 工具调用继续由 Runtime 的审批和执行链处理。
- 取消继续按 run_id 终止对应在途任务，并且不触发重试或候选降级。

### 6.2 provider 隔离

- 不同 provider 即使复用同一 driver，API Key、Base URL、限流桶、重试配置和熔断状态仍相互独立。
- 同一 provider 下的多个模型共享 provider 级限流和熔断状态。
- driver 名称不参与供应商限流和熔断身份计算。

### 6.3 reasoning 边界

- `native` 模式只读取供应商返回的 `reasoning_content`。
- `tags` 模式继续从 content 标签中解析 reasoning/response。
- `none` 模式将 content 全部视为 response。
- 本 PR 不根据模型名或 provider 名自动猜测 reasoning 模式。
- jojocode 模型使用何种 reasoning 模式，必须由其真实返回格式或供应商文档确认后配置。

### 6.4 失败语义

- 未知 provider、driver 或 model 是配置错误，不是可降级的供应商调用失败。
- 请求建立、认证、限流、流中断和取消继续使用现有代理语义。
- 只有产生非空 reasoning/response delta 才建立“可见输出已开始”边界；此后禁止重试或切换候选，避免拼接两个模型的输出。

## 7. 旧配置兼容

### 7.1 独立 Router 配置

`model_router_config.yaml` 缩进和字段完整性由配置加载器校验。该文件存在但 provider 缺少 driver 时直接报迁移错误，不猜测默认协议。

### 7.2 Legacy 配置转换

`_build_router_config_from_legacy()` 继续负责从旧 `config.yaml` 的 `llm.clients` 构建 `RouterConfig`（模型路由配置），并为转换出的 provider 显式写入 `openai_chat_completions`。

Legacy 转换是单向启动适配，不新增旧配置字段，也不允许新的 Router 配置依赖该兼容逻辑。

## 8. 测试计划

### 8.1 正常路径

- provider driver 和 model driver 覆盖均可正确加载。
- qwen、deepseek、openai、jojocode 都实例化 `OpenAIChatCompletionsClient`（OpenAI Chat Completions 协议客户端）。
- 不同 provider 复用同一客户端类型，但持有各自 API Key、Base URL 和 model id。
- 普通文本、native reasoning、tags reasoning、工具调用、finish reason 和 usage 继续正确归一化。
- embedding 继续返回向量。

### 8.2 配置错误

- 缺少 driver 在启动阶段失败。
- 未知 driver 在启动阶段失败。
- active 模型引用未知 provider 时失败。
- defaults 或 purpose 引用未知/disabled 模型时失败。
- driver 与模型 capabilities 不匹配时失败。
- 未展开的 API Key 占位符不能通过 active provider 校验。
- 不存在任何未知 provider 回退行为或相关误导日志。

### 8.3 provider 控制面

- 同一 provider 下多个模型共享限流和熔断状态。
- 不同 provider 使用相同 driver 时限流和熔断互不污染。
- YAML `circuit_breaker` 被完整加载，并按配置构造 `BreakerConfig`（熔断器配置）。
- provider retry 参数继续决定默认总尝试次数和退避因子。

### 8.4 流式可靠性回归

- 请求 timeout、首包 timeout 和后续流空闲 timeout 保持有界。
- 正常、异常、超时和取消都关闭 SDK stream。
- 取消不重试、不降级，也不计为 provider 失败。
- 两个并发调用的工具参数、finish reason、usage 和标签解析状态互不串线。
- 已产生可见输出后的断流不得切换模型。

### 8.5 Runtime 回归

- reasoning 不持久化，response 正确聚合。
- 工具调用、审批恢复、工具结果后的再次调用和最终回答闭环通过。
- CLI “思考/回答”展示及隐藏 reasoning 开关不变。
- Context compaction、Memory embedding 和模型降级相关测试通过。

### 8.6 配置 smoke

本 PR 的普通自动化测试不访问真实供应商。实现完成后可以使用最小非正式 smoke 验证当前 qwen、deepseek 和 jojocode 端点，但不得记录 API Key、完整敏感请求或响应，也不得把 smoke 写成正式可靠性或业务质量结论。

## 9. 实施顺序

1. **配置模型与校验**：增加 provider/model driver、修复 circuit breaker 加载、实现启动期引用检查，先补配置测试。
2. **通用协议客户端**：把 `openai_compat.py` 能力迁入 `OpenAIChatCompletionsClient`（OpenAI Chat Completions 协议客户端），保持现有公共 `LLMClient`（LLM 客户端抽象）和 `ChatChunk`（标准化聊天增量包）不变。
3. **Router 切换**：让 `ModelRouter`（模型路由器）按 effective driver 构造客户端，删除 provider 名称查实现和 Qwen 回退。
4. **配置迁移**：为 qwen、deepseek、openai、jojocode 显式声明 driver，更新 legacy 转换和示例配置。
5. **旧路径删除**：确认生产与测试调用归零后删除供应商客户端、注册表、自动发现和原通用实现文件。
6. **回归与文档**：执行 LLM/Runtime 定向回归、全量测试、compileall、diff check，并更新 LLM Wiki。

## 10. 旧路径迁移卡与删除条件

| 旧路径 | 唯一替代者 | 删除条件 |
|---|---|---|
| `providers/qwen.py` | `openai_chat_completions` driver | qwen 配置和客户端测试通过且零引用 |
| `providers/deepseek.py` | `openai_chat_completions` driver | deepseek 配置和客户端测试通过且零引用 |
| `providers/openai.py` | `openai_chat_completions` driver | openai 配置和客户端测试通过且零引用 |
| `providers/__init__.py` 注册/发现 | 显式 driver 构造映射 | Router、测试和文档不再调用 `register/get_provider` |
| 未知 provider → Qwen 回退 | 启动期配置错误 | 未知 provider/driver 测试通过 |
| `openai_compat.py` | `drivers/openai_chat_completions.py` | 功能和可靠性测试全部迁移 |

删除前执行：

```powershell
rg -n "QwenClient|DeepSeekClient|OpenAIClient|get_provider|register\(" src tests
rg -n "未知 provider.*Qwen|回退到 QwenClient" src tests
```

除迁移文档外不得存在生产或测试引用。

## 11. PR 验收标准

1. 所有 active 模型都能解析到显式、已注册且能力匹配的 driver。
2. qwen、deepseek、openai、jojocode 复用同一个通用 Chat Completions 客户端，不存在供应商空壳类。
3. 未知 provider、缺失 driver、未知 driver 和能力不匹配在启动阶段明确失败。
4. Runtime、Tool、Channel 的公开契约和持久化格式没有变化。
5. reasoning/response、工具调用、usage、embedding 行为保持不变。
6. timeout、取消、重试边界和 SDK stream 关闭保持现有可靠性。
7. 不同 provider 的连接配置、限流和熔断状态保持隔离。
8. `circuit_breaker` 配置加载缺口已修复并有回归测试。
9. 旧供应商客户端、旧注册表、自动发现和 Qwen 回退已物理删除，零引用搜索通过。
10. 本 PR 没有实现 Responses API 或 reasoning item，也没有为其修改 Runtime/Tool。
11. `tests/llm`、相关 `tests/runtime_v2`、全量 pytest、compileall 和 `git diff --check` 通过；若存在历史无关失败，单独列明，不恢复旧接口迎合测试。

## 12. 最终交付结果

```text
model
→ provider 配置
→ effective driver = openai_chat_completions
→ OpenAIChatCompletionsClient
→ ChatChunk
→ 现有 Runtime
```

本 PR 完成后仍不存在：Responses API 客户端、reasoning item 模型、response id 持久化、供应商服务端 conversation、OpenAI 托管工具或原生 Anthropic/Gemini 协议。未来只有在 Runtime、Tool 和持久化边界经过独立设计后，才重新评估 Responses API 迁移。
