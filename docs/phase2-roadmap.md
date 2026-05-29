# Phase 2 详细开发文档：多模型支持 + 基础设施

> 创建时间：2026-05-29
> 状态：已完成 ✅（实施日期：2026-05-29）

---

## 一、开发目的

将 LLM 层从单一 Qwen 客户端升级为多供应商架构，建立独立于具体实现的通用基础层（`common/`）。

**核心目标**：
1. LLM 客户端从 1 个 QwenClient 扩展为"1 个通用基类 + N 个具体客户端"
2. LLMProxy 从单模型路由升级为多供应商路由 + 跨供应商降级
3. 新增模型路由层（`model_router.py`），支持按用途（purpose）选择最优模型
4. 补充限流器、单例工具等通用基础设施

**设计原则**：AgentLoop 与 `LLMProxy.chat()` 的基本调用模式不变，但在调用时需显式传递 `purpose` 参数（当前固定为 `"chat"`，为后续阶段预留扩展接口）。AgentLoop 不感知供应商细节。

---

## 二、模块层级与依赖关系

Phase 2 新增和修改的模块按依赖关系分为 5 层。层级越高，依赖越多；同层内部无循环依赖。

```
Layer 1: common/              (无外部依赖)
   ↓
Layer 2: config/settings.py   (依赖 common/)
   ↓
Layer 3: llm/* 客户端         (依赖 base.py + config)
   ↓
Layer 4: proxy.py + router    (依赖 llm 客户端 + config)
   ↓
Layer 5: agent/loop.py        (依赖 proxy.py，微调)
```

### 2.1 Layer 1 — 通用工具库 `common/`

| 文件 | 状态 | 描述 |
|------|------|------|
| `common/__init__.py` | 新增 | 包初始化 |
| `common/rate_limiter.py` | 新增 | 令牌桶限流器，支持按模型/供应商维度的并发控制 |
| `common/singleton.py` | 新增 | 单例装饰器 |
| `common/utils.py` | 新增 | 通用工具函数（时间格式化、路径处理、字符串工具等） |

**依赖**：无外部依赖，不 import dotClaw 其他模块，可被任意模块导入。

### 2.2 Layer 2 — 配置系统

| 文件 | 状态 | 描述 |
|------|------|------|
| `config/settings.py` | 修改 | 新增 `ProvidersConfig`、`ModelsConfig`、`PurposesConfig` 等 dataclass；新增 `load_router_config()` 函数 |
| `model_router_config.yaml` | 新增 | 路由配置文件，放在**项目根目录**（与 `config.yaml` 同级），通过 `_find_project_root()` 定位。含 providers / models / purposes 三个 section。若文件不存在，自动从旧 `config.yaml` 的 `llm.clients` 构建等效配置 |

**依赖**：`common/utils.py`（`safe_load_yaml()`、`expand_env_vars()`）

**配置文件优先级规则**：

```
启动加载流程:
  1. 查找项目根目录下的 model_router_config.yaml
  2. 文件存在 → 解析新格式（providers / models / purposes）
  3. 文件不存在 → 调用 _build_router_config_from_legacy(config.llm)
     将旧格式（llm.clients）自动转换为新格式（RouterConfig）
     行为等价于 P1，保证后向兼容
```

**重要**：两个配置文件是**互斥使用**的关系，不同时生效。`model_router_config.yaml` 存在时完全使用新配置，`config.yaml` 的 `llm.clients` 不参与路由决策。不同供应商的 API key 使用各自的环境变量（`${QWEN_API_KEY}`、`${DEEPSEEK_API_KEY}` 等），互不干扰。

**配置结构概览**：

```yaml
# model_router_config.yaml（项目根目录，与 config.yaml 同级）

# Level 1: 全局默认
defaults:
  provider: qwen
  model: qwen3.5-flash
  parameters:
    temperature: 0.7
    max_tokens: 4096
  fallback_enabled: true

# Level 2: 供应商配置
providers:
  qwen:
    api_key: ${QWEN_API_KEY}
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    rate_limit:
      requests_per_minute: 500
    retry:
      max_attempts: 3
      backoff_factor: 2.0
  deepseek:
    api_key: ${DEEPSEEK_API_KEY}
    base_url: https://api.deepseek.com/v1
    rate_limit:
      requests_per_minute: 100
    retry:
      max_attempts: 2
  openai:
    api_key: ${OPENAI_API_KEY}
    base_url: https://api.openai.com/v1

# Level 3: 模型配置
models:
  qwen3.5-flash:
    provider: qwen
    model_id: qwen3.5-flash
    context_window: 32000
    capabilities: [chat, function_calling]
    status: active
  qwen-turbo:
    provider: qwen
    model_id: qwen-turbo
    context_window: 1000000
    capabilities: [chat, function_calling]
    status: active
  deepseek-v3:
    provider: deepseek
    model_id: deepseek-chat
    context_window: 64000
    capabilities: [chat, function_calling]
    status: active
  gpt-4o-mini:
    provider: openai
    model_id: gpt-4o-mini
    context_window: 128000
    capabilities: [chat, function_calling]
    status: active

# Level 4: 用途路由
purposes:
  chat:
    description: "日常对话"
    priority:
      - model: qwen3.5-flash
        priority: 1
      - model: deepseek-v3
        priority: 2
      - model: gpt-4o-mini
        priority: 3
```

> 注：Level 5（overrides）留到后续阶段实现。

### 2.3 Layer 3 — LLM 客户端层

| 文件 | 状态 | 描述 |
|------|------|------|
| `llm/base.py` | 不变 | `LLMClient(ABC)`、`Message`、`ChatChunk`、`ToolCall`、`ToolDefinition` |
| `llm/openai_compat.py` | **新增** | `OpenAICompatibleClient(LLMClient)` — 封装 OpenAI 兼容 API 通用逻辑 |
| `llm/qwen.py` | **重构** | 改为继承 `OpenAICompatibleClient`，只覆写 base_url 和 model 获取 |
| `llm/deepseek.py` | **新增** | `DeepSeekClient(OpenAICompatibleClient)` |
| `llm/openai.py` | **新增** | `OpenAIClient(OpenAICompatibleClient)` |
| `llm/proxy.py` | **重构** | 多供应商路由 + 降级逻辑；保留 P1 的重试机制 |

**继承关系**：
```
LLMClient (ABC)                    ← llm/base.py
  └── OpenAICompatibleClient       ← llm/openai_compat.py（新增）
       ├── QwenClient              ← llm/qwen.py（重构）
       ├── DeepSeekClient          ← llm/deepseek.py（新增）
       └── OpenAIClient            ← llm/openai.py（新增）
```

**类文件 vs 实例说明**：

- **类文件数 = 供应商数**（qwen.py、deepseek.py、openai.py），每个供应商一个类文件
- **实例数 = 模型数**，同一供应商的不同模型（如 qwen3.5-flash 和 qwen-turbo）使用**同一个 QwenClient 类的不同实例**，模型名在实例化时通过 `model_id` 传入
- 新增模型只需修改配置文件（`models` 节加一行），零代码改动
- `ModelRouter` 内部以 `model_name` 为 key 缓存 client 实例（懒加载，首次使用时创建）

**OpenAICompatibleClient 职责**：
- `chat()` 通用实现：消息格式转换 → 流式/非流式 API 调用 → tool_calls 参数累积 → 产出 ChatChunk
- `_convert_messages()` 通用消息格式转换
- `_parse_stream_chunk()` 通用流式 chunk 解析
- `_reset_stream_state()` 流式状态重置
- 子类只需覆写三个钩子方法：
  - `_get_api_key() -> str`：返回该 provider 的 API key
  - `_get_base_url() -> str`：返回该 provider 的 base URL
  - `_get_model_id() -> str`：返回当前实例绑定的 model 名称

**与 P1 的兼容**：所有 P1 修复的 bug（流式 tool_calls 参数累积、assistant(tool_calls) 消息序列化）均提取到 `OpenAICompatibleClient` 中，不丢失。

### 2.4 Layer 4 — 路由与代理层

| 文件 | 状态 | 描述 |
|------|------|------|
| `llm/model_router.py` | **新增** | 模型路由器：purpose → priority 确定性选择 + 降级链（从 priority 列表自动生成） |
| `llm/proxy.py` | **重构** | LLMProxy 重构为使用 ModelRouter |

**ModelRouter 职责**：
- `resolve(purpose: str, forced_model: str | None) -> tuple[LLMClient, str]`
  - forced_model 有值时按三层匹配规则找对应的 client 实例
  - forced_model 为 None 时查 `purposes[purpose].priority` → 按 priority 升序排列（数值越小越优先），取第一个 active model → 找到对应 client 实例
- `get_fallback_chain(purpose: str) -> list[str]`
  - 返回该 purpose 的降级模型列表（按 priority 升序排列，从 priority 列表自动生成，无需单独配置 fallback_chain 字段）
- `get_available_models() -> list[str]`
  - 返回所有 status=active 的模型名称（供 `/model` 命令）
- 客户端实例缓存：以 `model_name` 为 key，懒加载创建 client 实例。每个模型一个实例，同一供应商的不同模型共享同一个类但不同实例

**forced_model 三层匹配优先级**：
```
1. 精确匹配 models 字典的 key（如 "qwen3.5-flash"）→ 直接使用
2. 前缀匹配 providers 字典的 key（如 "qwen"）→ 使用该 provider 的 defaults.model
3. 都不匹配 → 日志 warning，降级到 defaults.provider + defaults.model
```

**LLMProxy 重构要点**：
- `__init__` 改为接收 `model_router: ModelRouter` + `rate_limiter: RateLimiter`
- `chat()` 签名增加 `purpose: str = "chat"` 参数
- `chat()` 流程：`rate_limiter.acquire(model)` → `router.resolve(purpose, forced_model)` → `client.chat()` → 异常处理
- `available_models` property 委托给 `router.get_available_models()`

**错误分类与降级策略**：

| 错误类型 | 何时触发 | 降级行为 |
|----------|---------|---------|
| `CallSetupError` | 连接超时、认证失败、DNS 解析失败、HTTP 非 2xx（流开始前） | 触发降级：切换到 fallback_chain 的下一个模型重试 |
| `NonRetryableStreamError` | 流式响应中途断连、chunk 解析失败、LLM 返回错误内容（流开始后） | **不降级**：直接向上抛出，由 AgentLoop 的异常处理逻辑捕获 |

降级规则：
- 只有 `CallSetupError` 触发降级（在 `async for chunk in client.chat()` 开始之前发生的异常）
- 一旦 `async for` 循环开始产出至少一个 chunk，后续的所有异常都是 `NonRetryableStreamError`，不再降级（因为已输出的 chunk 无法撤回给用户）
- 降级时**直接查 models 字典** + `_get_or_create_client()`，不经过 `router.resolve()`（避免 resolve 的 default-fallback 行为造成回环：fallback 到 defaults.model 可能恰好是已失败的模型）
- 降级链中的模型不在 models 字典中时 → 跳过（不回落 default），继续尝试下一个
- 降级链耗尽全部模型 → raise RuntimeError

### 2.5 Layer 5 — AgentLoop

| 文件 | 状态 | 描述 |
|------|------|------|
| `agent/loop.py` | 微调 | `self.llm.chat()` 调用增加 `purpose="chat"` 参数；其他逻辑不变，不感知供应商细节 |

**修改范围**（仅此一处）：
- `run()` 方法中 `self.llm.chat(...)` 调用行增加 `purpose="chat"` 关键字参数
- `_build_messages()`、工具调用循环、会话保存、调试追踪等全部不变
- AgentLoop 的 `__init__` 不变

---

## 三、各模块开发要点

### 3.1 `common/rate_limiter.py`

**要点**：
- 接口：`RateLimiter(rate_limit_configs: dict[str, RateLimitConfig])`，从各 provider 的 `rate_limit` 配置初始化
- `async acquire(model: str) -> None`：获取令牌，超出速率时 await 等待
- 使用令牌桶算法（Token Bucket），支持 burst
- 按 model 维度独立计数，内部按 provider 聚合（同一 provider 的所有 model 共享一个令牌桶）

**并发安全**：
- 内部使用 `asyncio.Lock` 保护 `refill` + `consume` 复合操作
- 并发调用允差 ±1 请求（asyncio 单线程下 Lock 有效，无需线程锁）

**配置来源**：
- Provider 的 `rate_limit` 配置由 `main.py` 在初始化时从 `RouterConfig.providers` 中提取，传递给 `RateLimiter`
- `requests_per_minute = 0` 表示不限流（默认值，开发环境无需配置）

**开发注意事项**：
- 默认关闭（`rate_limit.requests_per_minute = 0` 表示不限流），避免影响开发体验
- 令牌桶需要维护 `{provider: (tokens, last_refill_time)}` 状态，不是无状态的

### 3.2 `common/singleton.py`

**要点**：
- 提供 `@singleton` 装饰器或 `SingletonMeta` 元类
- 线程安全（如需要）
- 支持重置（for testing）

### 3.3 `common/utils.py`

**要点**：本文件禁止范围膨胀。Phase 2 阶段限定为以下两个函数：
- `expand_env_vars(value: Any) -> Any` — 环境变量展开（从 `config/settings.py` 提取，P1 已有实现）
- `safe_load_yaml(path: Path) -> dict` — YAML 安全加载封装（使用 `yaml.safe_load`，含文件不存在处理）

**开发注意事项**：不主动添加其他工具函数（时间格式化、路径处理等），等后续阶段有实际需要时再扩展。

### 3.4 `config/settings.py` — 配置扩展

**要点**：
- 新增 `ProviderConfig` dataclass：api_key, base_url, rate_limit, retry, extra_headers 等
- 新增 `ModelConfig` dataclass：provider, model_id, context_window, capabilities, status
- 新增 `PurposeConfig` dataclass：description + priority（`PurposePriority` 列表，`priority` 字段为 int 类型，数值越小越优先）。降级链由 priority 列表自动生成，不再单独配置 `fallback_chain`
- 新增 `RouterConfig` dataclass：聚合 defaults + providers + models + purposes
- 新增 `load_router_config(path: str) -> RouterConfig` 函数：读取并解析 `model_router_config.yaml`
- 新增 `_build_router_config_from_legacy(llm_config: LLMConfig) -> RouterConfig` 函数：将旧 `config.yaml` 的 `llm.clients` 格式自动转换为 `RouterConfig`，保证后向兼容

**后向兼容构建逻辑**（`_build_router_config_from_legacy`）：
```
输入：P1 的 LLMConfig（含 clients: {qwen3.5-flash: {...}, qwen-turbo: {...}}）
输出：完整的 RouterConfig

规则：
- defaults.provider = qwen（从第一个 client 的 provider 推断）
- defaults.model = llm_config.default_model
- providers: 从 clients 中提取 provider 名称，每个 provider 的 api_key/base_url 从第一个 model 取值
- models: 每个 client 映射为一个 model entry（model_id, provider, context_window 用默认值或从已有配置推断）
- purposes.chat.priority: 按 clients 的原有顺序列出，priority 依次为 1, 2, 3...
- purposes.chat 的降级链从 priority 列表自动生成（按 priority 升序排列所有 active model）
```

### 3.5 `llm/openai_compat.py` — 通用基类

**要点**：
- 从 `QwenClient` 提取的通用逻辑：
  - `_convert_messages()` — 消息格式转换（含 tool_calls 序列化）
  - `_parse_stream_chunk()` — 流式 chunk 解析 + tool_calls 参数累积
  - `_reset_stream_state()` — 流式状态重置
- 新增抽象钩子方法供子类覆写：`_get_api_key()`, `_get_base_url()`, `_get_model_id()`
- `_get_client()` — 创建 `AsyncOpenAI` 实例（子类可覆写以注入 custom headers）

### 3.6 `llm/qwen.py` — 重构为继承

**要点**：
- 删除所有通用逻辑（已提取到 openai_compat.py）
- 保留：`_get_api_key()` → 读 `config.providers.qwen.api_key`
- 保留：`_get_base_url()` → 读 `config.providers.qwen.base_url`
- 保留：`_get_model_id()` → 读当前 model 的 `model_id`

### 3.7 `llm/model_router.py` — 模型路由

**要点**：
- `resolve(purpose, forced_model)` 逻辑：
  1. forced_model 非空 → 按三层匹配优先级找到 model_name → 获取或创建 client 实例
  2. forced_model 为空 → 查 `purposes[purpose].priority` → 按 `priority` 升序排列（数值越小越优先），取第一个 active model → 找到对应 client 实例
- **确定性选择**（非随机）：`sorted(p.priority)` → 返回 priority 最小的 active model，同一 priority 时按配置顺序
- 降级链：`get_fallback_chain(purpose)` 从 priority 列表自动生成（按 priority 升序排列所有 active model），无需在配置文件中单独维护 `fallback_chain` 字段
- 客户端实例缓存：`_client_cache: dict[str, LLMClient]`，key 为 model_name。每个模型一个实例，同一供应商的不同模型使用相同类、不同实例

**forced_model 三层匹配优先级**（精确到行为）：
```
输入 forced_model → resolve 流程：

1. 精确匹配:
   forced_model in models 字典的 key → 直接使用该 model
   例: "qwen3.5-flash" → models["qwen3.5-flash"]

2. 前缀匹配:
   forced_model 是 providers 字典的 key → 使用该 provider 的 defaults.model
   例: "qwen" → providers["qwen"] + defaults.model = "qwen3.5-flash"

3. 默认降级:
   都不匹配 → 日志 warning → 使用 defaults.provider + defaults.model
   例: "unknown" → defaults.provider + defaults.model
```

**开发注意事项**：
- 前缀匹配规则：按 provider 名称在 `providers` dict 中查找，然后在该 provider 的 models 中匹配
- 客户端实例缓存：`_client_cache` 以 model 名称为 key，首次访问时懒加载。实例创建调用 `_instantiate_client(provider_config, model_config)` 工厂函数
- `_instantiate_client` 通过字典查表选择具体 Client 类，未知 provider 名称回退到 QwenClient（通用 OpenAI 兼容客户端）

### 3.8 `llm/proxy.py` — 重构代理

**要点**：
- `__init__` 改为接收 `model_router: ModelRouter` + `rate_limiter: RateLimiter`
- P1 的 `_clients` dict 删除，client 管理委托给 `ModelRouter`
- `chat()` 签名变为 `async chat(messages, tools=None, model=None, purpose="chat", stream=True)`
- `chat()` 新流程：
  1. `await rate_limiter.acquire(model)` — 限流检查
  2. `client, resolved_model = router.resolve(purpose, model)` — 路由
  3. 尝试 `client.chat()`：
     - `CallSetupError` → 获取 `router.get_fallback_chain(purpose)` → 列表非空则重试下一个模型 → 列表空则 raise
     - `NonRetryableStreamError` → 直接 raise（不降级）
  4. 全部 fallback 耗尽 → raise RuntimeError
- 保留 P1 的 `_max_retries` 和 `_base_delay` 用于单次调用内的重试（与降级是两个层面：retry 是同一模型重试，fallback 是切换模型）

**异常类定义**：

`CallSetupError(Exception)`：在 `async for` 循环开始前发生的异常
- 触发条件：连接超时、HTTP 非 2xx（流开始前）、认证失败（401/403）、DNS 解析失败
- 降级行为：触发降级到 fallback_chain 的下一个模型

`NonRetryableStreamError(Exception)`：在 `async for` 循环已产出至少一个 chunk 后发生的异常
- 触发条件：流式中途断连、chunk 解析失败、流式阶段 HTTP 错误
- 降级行为：不降级，直接向上抛出（已输出的 chunk 无法撤回给用户）

**开发注意事项**：
- 流式阶段：`async for chunk in client.chat()` 一旦开始迭代，整个循环包在 try/except 中，捕获的异常都是 `NonRetryableStreamError`
- 调用前阶段：`client.chat()` 返回 async iterator 之前的连接建立阶段，异常是 `CallSetupError`
- 保留 P1 的重试机制（`_max_retries` + 指数退避）用于 `CallSetupError` 的同一模型重试

### 3.9 `agent/loop.py` — 微调

**要点**：
- `run()` 方法中 `self.llm.chat(...)` 调用行增加 `purpose="chat"` 关键字参数
- 其他所有逻辑不变：`_build_messages()`、工具调用循环、会话保存、调试追踪全部保持 P1 行为
- AgentLoop 的 `__init__` 不变，不新增任何属性

---

## 四、开发实施顺序（按依赖关系）

```
Step 1: common/          (无依赖，先做)
Step 2: 配置文件          (依赖 common/)
Step 3: openai_compat.py  (依赖 base.py)
Step 4: 重构 qwen.py      (依赖 openai_compat.py)
Step 5: 新增客户端         (依赖 openai_compat.py，Step 4 验证完后并行)
Step 6: model_router.py   (依赖 config + 客户端)
Step 7: 重构 proxy.py      (依赖 model_router + common/)
Step 8: 更新 main.py       (初始化 ModelRouter + RateLimiter + LLMProxy)
Step 9: 验收测试             (手动 CLI + 自动化测试)
```

Step 3-4 必须串行（先建基类再重构），Step 5 的三个客户端可并行（都只依赖基类）。Step 2 可与 Step 1 并行。

**Step 8 详细说明 — main.py 初始化迁移**：

`_run_cli()` 中 `LLMProxy` 的初始化代码需要从：

```python
# P1（当前）
llm_proxy = LLMProxy(config.llm)
```

改为以下逻辑：

```python
# P2（新）
from dotclaw.llm.model_router import ModelRouter
from dotclaw.common.rate_limiter import RateLimiter

# 1. 加载路由配置（优先新格式，降级到旧格式）
project_root = _find_project_root()
router_config_path = project_root / "model_router_config.yaml"

if router_config_path.exists():
    router_config = load_router_config(str(router_config_path))
else:
    # 后向兼容：从旧 config.llm 构建 RouterConfig
    router_config = _build_router_config_from_legacy(config.llm)

# 2. 初始化 ModelRouter
model_router = ModelRouter(router_config)

# 3. 初始化 RateLimiter（从各 provider 配置提取 rate_limit）
rate_limit_configs = {
    name: prov_cfg.rate_limit
    for name, prov_cfg in router_config.providers.items()
}
rate_limiter = RateLimiter(rate_limit_configs)

# 4. 初始化 LLMProxy
llm_proxy = LLMProxy(
    model_router=model_router,
    rate_limiter=rate_limiter,
)
```

**关键点**：
- `_find_project_root()` 和 `_build_router_config_from_legacy()` 从 `config/settings.py` 导入
- `load_router_config()` 从 `config/settings.py` 导入
- `RateLimiter` 接收 `dict[str, RateLimitConfig]`，按 provider 维度初始化令牌桶
- 后向兼容路径自动将旧 `llm.clients` 格式转换为 `RouterConfig`，用户无需修改 `config.yaml`

---

## 五、验收标准

### 5.1 功能验收

**场景 1：单供应商纯文本对话**
- 启动 `dotclaw`，`/model qwen3.5-flash`
- 输入 `你好`
- 预期：QwenClient 被调用，返回文本回复，流式输出正常

**场景 2：模型切换**
- `/model deepseek-v3`
- 输入 `你好`
- 预期：DeepSeekClient 被调用，回复正常（不需要重启）

**场景 3：跨供应商降级**
- 配置中设置 deepseek 的 base_url 为无法访问的地址
- `/model deepseek-v3`
- 输入 `你好`
- 预期：DeepSeekClient 调用失败 → 降级到 fallback_chain 的下一个模型（如 qwen-turbo）→ 返回正常回复

**场景 4：多轮工具调用**
- 使用任意模型，输入需要工具调用的问题（如 `现在几点了？`）
- 预期：LLM 返回 tool_call → 执行工具 → 结果返回 LLM → 最终文本

**场景 5：限流保护**
- 配置 `rate_limit.requests_per_minute = 2`
- 快速连续发送 3 条消息
- 预期：前 2 条正常处理，第 3 条等待直到令牌恢复

**场景 6：后向兼容**
- 使用旧配置（`llm.clients` 格式，非 `model_router_config.yaml`）
- 预期：LLMProxy 自动降级到 P1 行为，不报错

### 5.2 代码质量验收

- `OpenAICompatibleClient` 的 `chat()` 所产生的行为与 P1 的 `QwenClient.chat()` 完全一致
- QwenClient 重构后，所有 P1 验收场景测试通过
- `common/` 模块零外部依赖（`import dotclaw` 测试失败）
- `AgentLoop` 仅修改一行（增加 `purpose="chat"`）

### 5.3 自动化测试

新增 `tests/test_phase2_acceptance.py`，覆盖以下 7 个场景（使用 MockLLM + FakeChannel 基础设施）：

| # | 测试场景 | 验证内容 |
|---|---------|---------|
| 1 | OpenAICompatibleClient 等价性 | Mock API 响应，验证 chat() 产出与 P1 QwenClient 完全一致（消息格式、tool_calls 序列化、流式 chunk 顺序） |
| 2 | ModelRouter 优先级制选择 | 验证 100 次 resolve 确定性返回 priority 最小的 model；验证降级链按 priority 升序排列 |
| 3 | ModelRouter forced_model 匹配 | 验证精确匹配、前缀匹配、不存在降级三层行为 |
| 4 | Proxy 降级链（实际 API） | 构造无效 base_url → 第一个 model 失败 → 按 priority 降级到下一个 model → 成功返回 |
| 5 | RateLimiter 基本行为 | 验证 acquire 在配额内立即返回；超配额时触发 asyncio.sleep（mock） |
| 6 | Proxy 流式中途不降级 | 模拟 client.chat() 产出 chunk 后抛出异常，验证不触发 fallback（NonRetryableStreamError 向上传播） |
| 7 | Proxy 调用前失败降级 | 模拟 client.chat() 在开始前抛出 CallSetupError，验证降级到 priority 列表的下一个 model |

### 5.4 手动测试

- 按 5.1 的 6 个场景逐一在 CLI 中验证
- P1 回归测试：`pytest tests/test_phase1_acceptance.py -v` 全部通过
- Phase 2 自动化测试：`pytest tests/test_phase2_acceptance.py -v` 全部通过

---

## 六、开发注意事项

1. **P1 回归保护**：重构 `QwenClient` 后必须确保 P1 的 5 个验收场景全部通过。Step 4 完成后立即运行 P1 测试
2. **配置后向兼容**：`model_router_config.yaml` 不存在时，LLMProxy 应自动使用 `config.yaml` 的 `llm.clients` 旧格式，通过 `_build_router_config_from_legacy()` 构建等效 `RouterConfig`
3. **限流器默认关闭**：`rate_limit.requests_per_minute = 0` 表示不限流，开发环境不需要配置
4. **消息格式不变**：`Message` 和 `ChatChunk` 不做任何修改，所有适配在 Client 层完成
5. **前缀匹配容错**：model 名称未匹配到任何 provider 时，按三层匹配规则最终降级到 `defaults.provider` 和 `defaults.model`，并写入 warning 日志。注意此行为仅在 `resolve()` 中，降级链不走此路径（直接查 models 字典）
6. **日志记录**：每次 provider 切换和降级尝试都写入 debug log（`logging.getLogger("dotclaw.llm")`），方便调试
7. **流式降级边界**：`async for` 循环一旦开始迭代，异常不再触发降级（已输出 chunk 无法撤回）。开发和测试时特别注意这个边界
8. **RateLimiter 并发安全**：token bucket 的 `refill + consume` 是复合操作，使用 `asyncio.Lock` 保护。并发调用的限流允差为 ±1 请求
9. **实例缓存 vs 类文件**：新增模型只需改配置文件（`models` 节加一行），无需新建类文件。同一供应商的不同模型使用相同类、不同实例
10. **自动化测试先行**：Step 3-5 每个客户端完成后即写对应测试，不集中在 Step 9 补课

---

## 七、文件清单

| 文件 | 状态 | 估计复杂度 |
|------|------|-----------|
| `common/__init__.py` | 新增 | 低 |
| `common/rate_limiter.py` | 新增 | 中 |
| `common/singleton.py` | 新增 | 低 |
| `common/utils.py` | 新增 | 低 |
| `config/settings.py` | 修改 | 高 |
| `model_router_config.yaml` | 新增 | 中 | 项目根目录，与 config.yaml 同级 |
| `tests/test_phase2_acceptance.py` | 新增 | 高 |
| `llm/openai_compat.py` | 新增 | 高 |
| `llm/qwen.py` | 重构 | 中 |
| `llm/deepseek.py` | 新增 | 低 |
| `llm/openai.py` | 新增 | 低 |
| `llm/model_router.py` | 新增 | 高 |
| `llm/proxy.py` | 重构 | 高 |
| `main.py` | 修改 | 中 |
| `config.yaml` | 修改 | 低 |
