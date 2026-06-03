# Phase 2 开发计划审计报告

> 审计人：开发架构师
> 审计日期：2026-05-29
> 审计对象：`docs/phase2-roadmap.md` — Phase 2 详细开发文档
> 状态：✅ 已查阅（2026-05-29）

**查阅结果**：

6 个阻塞级缺陷中：同意 5 个，不同意 1 个（缺陷 4，详见下方）。
4 个设计缝隙中：同意 4 个。
缺失项清单：同意 8 项。

详细回应已口头告知计划人员，将在 `phase2-roadmap.md` 中更新以下内容：
1. 增加 `LLMProxy.chat(purpose)` 参数，AgentLoop 显式传递 → 修复缺陷 1
2. ModelRouter 改为 per-model 缓存粒度 → 修复缺陷 2
3. 新增 CallSetupError / NonRetryableStreamError 异常分类 → 修复缺陷 3
4. 补充双配置文件降级逻辑说明 → 回应缺陷 4（不同意合并，但补充优先级规则）
5. RateLimiter 使用 asyncio.Lock → 修复缺陷 5
6. 新增 tests/test_phase2_acceptance.py 测试计划 → 修复缺陷 6
7-10: 补充 forced_model 匹配、utils.py 范围、路径约定、main.py 初始化伪代码 → 修复缝隙 7-10

---

## 总体评价

计划整体结构清晰，5 层分层依赖设计合理，`OpenAICompatibleClient` 提取方案是正确的技术方向。但存在 **6 个阻塞级缺陷** 和 **若干设计缝隙**，必须在编码前解决。

---

## 一、关键缺陷（🔴 阻塞级，必须在编码前修正）

### 缺陷 1：`AgentLoop` 与 `purpose` 路由存在根本矛盾

**位置**：Phase 2 路线图 §2.4、§2.5、§3.7

**问题描述**：

路线图反复强调"AgentLoop 接口不变"（§2.5），但 `ModelRouter` 的核心方法是 `resolve(purpose, forced_model)`，引入了 `purpose` 路由概念。当前 `AgentLoop` 只传递 `model=self.model`，完全没有 `purpose` 的概念：

```python
# 当前 AgentLoop (agent/loop.py:80-84)
async for chunk in self.llm.chat(
    messages=messages,
    tools=self._tool_registry.get_definitions() if self._tool_registry else None,
    model=self.model,       # ← 只有 model，没有 purpose
    stream=self.config.llm.stream,
):
```

而当前 `main.py` 中 `/model` 命令的行为是直接设置 `agent.model = args`。在新架构下，`/model deepseek-v3` 到底意味着什么——设置 `forced_model`？改变 `purpose`？还是两者都是？

**影响**：如果 AgentLoop 不传 `purpose`，`ModelRouter` 无法进行用途路由，`purposes` 配置节形同虚设。

**改进建议**：

1. 在 `LLMProxy.chat()` 签名中显式增加 `purpose: str = "chat"` 参数
2. 保留 `model` 参数用于 /model 命令的强制指定（override 行为）
3. 在 `AgentLoop` 中显式传递 `purpose`（当前只有 "chat"，但为后续扩展留接口）
4. 更新路线图 §2.5 中的"不变"描述为"接口签名微调，但 AgentLoop 不感知供应商细节"

---

### 缺陷 2：`OpenAICompatibleClient` 单实例 vs 多模型共存矛盾

**位置**：Phase 2 路线图 §2.3、§3.5、§3.7

**问题描述**：

路线图 §3.7 规定"客户端缓存：同一 provider 的 client 只创建一次（懒加载）"，即一个 `QwenClient` 实例同时服务 `qwen3.5-flash` 和 `qwen-turbo`。但当前客户端在构造时绑定模型：

```python
# 当前 QwenClient (llm/qwen.py:21-28)
def __init__(self, api_key, base_url, model="qwen-plus"):
    self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    self._model = model  # ← 构造时绑定！无法动态切换
```

虽然计划提出 `_get_model_id()` 钩子可以返回动态值，但以下问题仍未解决：

- `context_window` 等模型级属性如何获取？（`qwen3.5-flash` 有 32K，`qwen-turbo` 有 1M）
- context_window 是 Phase 3 上下文裁剪模块的关键依赖，单实例无法区分

**影响**：阻塞当前设计，且影响 Phase 3 的上下文管理功能。

**改进建议**：

**方案 A（推荐）**：改 `ModelRouter` 缓存粒度为 **per-model**（每个 model 一个 client 实例）

- 优点：每个实例绑定明确的 model，context_window 等属性自然区分，子类实现简单
- 缺点：client 实例数 = model 数（当前 4 个 model，可接受）

**方案 B**：给 `chat()` 方法增加 `model` 参数，所有模型相关属性改为动态方法

- 优点：节省少量内存
- 缺点：子类实现复杂，需要重构 context_window 等属性的访问方式

建议选择 **方案 A**，用清晰换简单。

---

### 缺陷 3：流式响应中途失败的重试策略未定义

**位置**：Phase 2 路线图 §3.8

**问题描述**：

`LLMProxy.chat()` 是一个 `async generator`，路线图 §3.8 描述的新流程为：

```
await rate_limiter.acquire(model)
→ router.resolve()
→ client.chat()   ← async generator，开始 yield chunks
→ 异常时 → fallback_chain → 下一个模型重试
```

但在流式场景中，如果 `client.chat()` 在已经产出若干 chunk 后再抛出异常：

1. 前几个 chunk 已经被 `AgentLoop` 流式输出给用户了
2. 降级重试从新模型从头开始，用户会看到重复内容
3. 更重要的是：已经部分发送给用户的 chunks **无法撤回**

**影响**：用户体验严重受损——看到半截回答后突然重新开始。

**改进建议**：

**采用方案 A（保守且安全）**：

- 流式阶段（已经开始 yield chunks 后）**不降级**
- 只有在调用前阶段失败（连接失败、认证失败、超时）才触发降级
- 流式中途异常直接向上抛出，由 AgentLoop 处理

具体实现：区分两类错误——

```python
class NonRetryableStreamError(Exception):  # 流式中途异常，不降级
    pass

class CallSetupError(Exception):            # 调用前异常，可降级
    pass
```

并更新路线图 §3.8 的伪代码以反映这一策略。

---

### 缺陷 4：双配置文件冲突

**位置**：Phase 2 路线图 §2.2、§3.4

**问题描述**：

路线图引入了新配置文件 `config/model_router_config.yaml`，而现有 `config.yaml` 中已有 `llm.clients` 节。两套配置存在冲突：

| 维度 | config.yaml | model_router_config.yaml |
|------|------------|--------------------------|
| API Key 环境变量 | `${DOTCLAW_API_KEY}` | `${QWEN_API_KEY}` |
| 模型列表 | `llm.clients` | `models` |
| 降级链 | `llm.fallbacks` | `purposes.*.fallback_chain` |

**问题**：

- 如果两个文件同时存在，`api_key` 从哪个环境变量读取？用户需要配几个环境变量？
- 旧 `config.yaml` 的 `llm.clients` 节变成死代码，但用户无法直观判断
- `${DOTCLAW_API_KEY}` 和 `${QWEN_API_KEY}` 是两个不同的环境变量名，容易配错

**影响**：用户困惑，运维负担加重。

**改进建议**：

**方案 A（推荐）**：将 `providers / models / purposes` 合并进现有 `config.yaml` 的 `llm` 节

- 一个文件管理所有配置，消除多文件维护成本
- 唯一需要独立的是 `model_router_config.yaml` 不存在时的向后兼容逻辑（直接 fallback 到旧 `llm.clients` 格式）

**方案 B**：保留双文件，但严格定义优先级

1. `model_router_config.yaml` 存在 → 完全使用新配置，忽略 `config.yaml` 的 `llm.clients`
2. 不存在 → 使用旧 `config.yaml` 的 `llm.clients` 格式
3. 统一环境变量命名：新配置也兼容 `${DOTCLAW_API_KEY}`（各 provider 的 `api_key` 可 override）

---

### 缺陷 5：`RateLimiter` 的异步并发安全性未考虑

**位置**：Phase 2 路线图 §3.1

**问题描述**：

路线图 §3.1 称 `RateLimiter` 是"无状态的纯算法实现"，但令牌桶（Token Bucket）算法必然是有状态的——它需要维护当前的令牌数和上次补充时间：

```python
class RateLimiter:
    def __init__(self):
        self._tokens: dict[str, float] = {}       # 可变状态
        self._last_refill: dict[str, float] = {}   # 可变状态

    async def acquire(self, model: str):
        # 如果有 3 个并发的 chat() 同时调用 acquire()
        # asyncio 单线程下，非 await 的赋值是原子的
        # 但 refill + consume 之间如果夹了 await，可能出偏差
```

虽然 asyncio 单线程保证了赋值操作的原子性，但在 `refill → consume` 的复合操作中如果有 `await` 点（如 `await asyncio.sleep()` 做限流等待），其他协程可能插入并修改令牌数。

**影响**：高并发场景下可能出现 ±N 的计数偏差，限流不够精确。

**改进建议**：

1. 使用 `asyncio.Lock` 保护令牌桶的 refill + consume 操作
2. 在路线图 §3.1 中明确标注并发安全策略：
   - "Token Bucket 使用 `asyncio.Lock` 保护，并发调用允差 ±1 请求"
3. 增加并发安全测试用例

---

### 缺陷 6：测试策略缺失

**位置**：Phase 2 路线图 §5.3

**问题描述**：

路线图 §5.3 的测试方式仅为"手动 CLI 测试 + P1 回归测试"，完全缺少以下测试：

| 缺失的测试 | 重要性 |
|-----------|--------|
| `OpenAICompatibleClient` 单元测试（验证提取的通用逻辑与 P1 QwenClient 行为等价） | 🔴 必须 |
| `ModelRouter` 单元测试（加权随机分布、降级链、前缀匹配、forced_model） | 🔴 必须 |
| `RateLimiter` 单元测试（令牌消耗、限流行为、并发安全） | 🟡 重要 |
| `proxy.py` 重构后行为测试（降级触发、已消费 chunk 不回滚） | 🔴 必须 |
| `config/settings.py` 新 dataclass 解析测试 | 🟡 重要 |

**影响**：没有自动化测试，重构风险极高。`OpenAICompatibleClient` 提取逻辑是否正确、`ModelRouter` 加权随机是否符合预期，全靠手动验证。

**改进建议**：

新增 `tests/test_phase2_acceptance.py`，至少覆盖：

1. **场景 1 — OpenAICompatibleClient 等价性**：Mock API，验证 chat() 产出与 P1 QwenClient 完全一致
2. **场景 2 — ModelRouter 加权随机**：模拟 1000 次 resolve，验证分布接近权重比例
3. **场景 3 — ModelRouter 降级链**：验证 get_fallback_chain 返回正确顺序
4. **场景 4 — ModelRouter forced_model 匹配**：验证精确匹配、前缀匹配、未匹配降级
5. **场景 5 — RateLimiter 基本行为**：验证 acquire 等待、令牌恢复
6. **场景 6 — Proxy 降级不重复**：模拟流式中途失败，验证不降级到第二个模型
7. **场景 7 — Proxy 调用前失败降级**：模拟连接失败，验证降级到 fallback model

利用现有的 `MockLLM` 和 `FakeChannel` 测试基础设施。

---

## 二、设计缝隙（🟡 重要，实施中需关注）

### 缝隙 7：`forced_model` 前缀匹配逻辑模糊

**位置**：Phase 2 路线图 §3.7

**问题**：`resolve(purpose, forced_model)` 中 `forced_model` 的处理只有一句话"前缀匹配 providers"，但不同输入的处理规则不明确：

| 输入 | 预期行为 | 实际是否明确？ |
|------|---------|---------------|
| `"qwen3.5-flash"` | 精确匹配 models dict | ✅ 明确 |
| `"qwen"` | 前缀匹配 provider=qwen，哪个 model？ | ❌ 不明 |
| `"deepseek"` | 前缀匹配 provider=deepseek，哪个 model？ | ❌ 不明 |
| `"unknown"` | 完全不匹配 | ❌ 不明 |

**改进建议**：在路线图 §3.7 中明确三层匹配优先级：

```
1. 精确匹配 models 字典的 key
2. 前缀匹配 providers 字典的 key → 使用 defaults.model
3. 都失败 → 使用 defaults.provider + defaults.model（记录 warning 日志）
```

---

### 缝隙 8：`common/utils.py` 范围膨胀风险

**位置**：Phase 2 路线图 §3.3

**问题**：`utils.py` 是行业中臭名昭著的"垃圾桶"文件。计划列了"时间格式化、路径处理、字符串工具等"，但实际从 `config/settings.py` 提取的只有 `_expand_env()` 一个明确函数。

**改进建议**：在路线图 §3.3 中明确 hard limit：

```
common/utils.py 仅包含：
- expand_env_vars(value) -> Any   （从 config/settings.py 提取）
- safe_load_yaml(path) -> dict    （YAML 安全加载封装）

不主动添加其他工具函数，等实际需要时再扩展。
```

---

### 缝隙 9：配置文件路径约定不一致

**位置**：Phase 2 路线图 §2.2

**问题**：路线图写 `config/model_router_config.yaml`，但 `config/` 既是 Python 包的子目录（`src/dotclaw/config/`），又可能是项目根的配置目录。当前 `config.yaml` 在项目根目录。

**改进建议**：明确 `model_router_config.yaml` 放在**项目根目录**（与 `config.yaml` 同级），通过 `_find_project_root()` 定位加载。

---

### 缝隙 10：`LLMProxy.__init__` 签名变化对初始化代码的影响未详细说明

**位置**：Phase 2 路线图 §4 Step 8

**问题**：Step 8 "更新 main.py" 只有一句话，但变更量很大：

```python
# P1 (当前):
llm_proxy = LLMProxy(config.llm)

# P2 (计划):
llm_proxy = LLMProxy(model_router, rate_limiter)
```

缺少：
- `RateLimiter` 从哪里获取各 provider 的 `rate_limit` 配置？
- `RateLimiter` 是全局一个还是每个 provider 一个？
- 向后兼容路径（无 `model_router_config.yaml`）下如何初始化 `ModelRouter`？

**改进建议**：在路线图中补充 Step 8 的伪代码：

```python
# === P2 main.py 初始化伪代码 ===

if (project_root / "model_router_config.yaml").exists():
    router_config = load_router_config("model_router_config.yaml")
else:
    router_config = _build_router_config_from_legacy(config.llm)

model_router = ModelRouter(router_config)

# 从 router_config.providers 中提取各 provider 的 rate_limit 配置
rate_limit_configs = {
    name: cfg.rate_limit for name, cfg in router_config.providers.items()
}
rate_limiter = RateLimiter(rate_limit_configs)

llm_proxy = LLMProxy(model_router=model_router, rate_limiter=rate_limiter)
```

---

## 三、缺失项清单

| # | 缺失项 | 影响级别 | 建议补充位置 |
|---|--------|---------|------------|
| 1 | `chat()` 方法是否需增加 `model` 参数以支持动态切换 | 🔴 阻塞 | §3.5 |
| 2 | `context_window` 等模型属性的存取方式（per-instance vs dynamic） | 🔴 阻塞 | §3.5 |
| 3 | `RateLimiter` 配置来源（从 provider config 提取） | 🟡 重要 | §3.1 |
| 4 | 错误类型分类：哪些异常可重试、哪些直接抛出 | 🟡 重要 | §3.8 |
| 5 | `main.py` 初始化迁移详细代码 | 🟡 重要 | §4 Step 8 |
| 6 | 新增模块的 `__init__.py` 导出约定 | 🟢 建议 | §3 各模块开发要点 |
| 7 | `model_router_config.yaml` 不存在时的降级构建逻辑 | 🟡 重要 | §3.4 |
| 8 | 各 `LLMClient` 子类是否需要支持自定义 HTTP headers | 🟢 建议 | §3.6 |

---

## 四、改进建议汇总

### 必须修改（阻塞开发启动）

| # | 建议 | 影响位置 |
|---|------|---------|
| 1 | 在 `LLMProxy.chat()` 签名中增加 `purpose` 参数，AgentLoop 显式传递 | §2.4, §2.5, §3.8 |
| 2 | 改 ModelRouter 缓存粒度为 per-model（每个 model 一个 client 实例） | §2.3, §3.7 |
| 3 | 明确流式中途失败不降级，区分 `CallSetupError` 和 `NonRetryableStreamError` | §3.8 |
| 4 | 合并 providers/models/purposes 到 `config.yaml`，或严格定义优先级 | §2.2, §3.4 |
| 5 | RateLimiter 使用 `asyncio.Lock` 保护内部状态 | §3.1 |
| 6 | 新增 `tests/test_phase2_acceptance.py`，覆盖 7 个场景 | §5 |

### 建议修改（提升实施质量）

| # | 建议 | 影响位置 |
|---|------|---------|
| 7 | 明确 `forced_model` 三层匹配优先级 | §3.7 |
| 8 | 限制 `common/utils.py` 的函数范围 | §3.3 |
| 9 | 明确 `model_router_config.yaml` 放在项目根目录 | §2.2 |
| 10 | 补充 Step 8 main.py 迁移伪代码 | §4 |
| 11 | 补充错误类型分类和重试策略表 | §3.8 |
| 12 | 各模块补充 `__init__.py` 导出约定 | §3 |

---

## 五、值得肯定的设计

以下设计点值得保留，体现了良好的工程意识：

1. **5 层分层依赖**：依赖方向单向向下，设计干净，符合依赖倒置原则
2. **`OpenAICompatibleClient` 提取方案**：通用逻辑集中，子类薄封装，避免了代码重复
3. **P1 bug 修复不丢失**：流式 tool_calls 参数累积、消息序列化均提取到基类，重构安全
4. **限流器默认关闭**：`rate_limit.requests_per_minute = 0` 表示不限流，开发体验友好
5. **P1 回归测试先行**：Step 4 完成后立即运行 P1 测试，提供了安全网
6. **Step 5 三个客户端可并行开发**：认识到了独立任务的并行性
7. **配置后向兼容意识**：考虑了旧用户从 P1 升级的平滑路径

---

> **给计划人员的行动项**：请逐一审计上述 6 个阻塞级缺陷，更新 `phase2-roadmap.md` 中对应章节。建议先解决缺陷 1-4（架构层面），再细化缺陷 5-6（实现层面），然后补充缺失项和设计缝隙的细节。
