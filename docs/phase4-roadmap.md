# Phase 4 详细开发文档：记忆系统

> 创建时间：2026-05-30
> 修订时间：2026-05-31
>   - v2：融入 SQLite+FTS5+向量检索引擎设计，参考 mem0/CowAgent 记忆层
>   - v4：实施完成，状态更新（实际完成日期：2026-05-31~06-01）
> 状态：已完成 ✅

---

## 一、开发目的

P4 是 P3 基础设施的**第一个消费者**——让 Agent 拥有持久记忆能力。

**核心目标**：
1. 三级记忆架构：短期（Session.messages，P1 已有）→ 中期（日记忆文件）→ 长期（MEMORY.md + Deep Dream 蒸馏）
2. 混合检索引擎：SQLite FTS5（unicode61 + trigram 双索引）+ 向量余弦相似度，替代旧计划的硬截断方案
3. MemoryProvider 激活：P3 预留的骨架从返回 `None` 变为语义检索注入
4. Token 精确计算：tiktoken 替换 P3 的中英文差异化估算公式

**设计原则**：
- AgentLoop 不引入 AgentServices 聚合类——保持显式传参，Python kwargs 已足够清晰
- 日摘要通过后台 `asyncio.create_task` 异步执行，不阻塞 CLI 提示符返回
- 核心逻辑自研（检索、流转、蒸馏），基础设施用成熟方案（SQLite FTS5、numpy 向量计算）
- 单用户模型，不引入 scope/user_id 体系——保持学习框架的简洁

---

## 二、架构总览

```
┌─────────────────────────────────────────────────────────┐
│                      AgentLoop                          │
│  run() → _build_context()[async] → PromptBuilder        │
│            │                                            │
│            ▼                                            │
│    AgentContext.memory_summary (语义检索结果)            │
└──────────────┬──────────────────────┬───────────────────┘
               │                      │
               ▼                      ▼
     ┌─────────────────┐   ┌──────────────────┐
     │  MemoryProvider  │   │  MemoryManager   │
     │  (PromptBuilder) │   │  (核心调度)       │
     └────────┬────────┘   └──┬───┬───┬───────┘
              │               │   │   │
              ▼               ▼   ▼   ▼
    ┌──────────────┐   ┌────────┐ ┌──────────┐ ┌──────────┐
    │ search(query) │   │ Flush  │ │  Dream   │ │  Sync    │
    │ 语义检索注入   │   │ L2 写入│ │ L3 蒸馏  │ │ 索引同步  │
    └──────────────┘   └───┬────┘ └────┬─────┘ └────┬─────┘
                           │           │             │
                           ▼           ▼             ▼
                  ┌──────────────────────────────────────┐
                  │          MemoryStorage               │
                  │   SQLite (WAL) + FTS5 双索引         │
                  │   + embedding BLOB + 文件元数据       │
                  └──────────────────────────────────────┘
```

**数据通路（单一路径，消除旧版的多路不一致）**：

```
AgentLoop._build_context(user_message)  [async]
    └─ await MemoryManager.search(user_message)
         └─ 混合检索结果 → 拼接为 memory_summary 文本
              └─ AgentContext(memory_summary=...)
                   └─ MemoryProvider.provide(context)  [同步]
                        └─ 从 context.memory_summary 读取 → PromptBuilder 注入
```

---

## 三、模块层级与依赖关系

Phase 4 新增和修改的模块按依赖关系分为 5 层。

```
Layer 1: MemoryStorage + TextChunker      ← 基础层：SQLite 建表、FTS5、分块
   ↓
Layer 2: EmbeddingProvider                ← 向量嵌入（依赖 OpenAI-compatible API）
   ↓
Layer 3: MemoryManager                    ← 核心调度：混合检索 + sync + flush 触发
   ↓
Layer 4: MemoryFlushManager + DeepDream   ← L2 写入 + L3 蒸馏
   ↓
Layer 5: MemoryProvider（激活）            ← PromptBuilder 注入
         message_utils（修改）             ← tiktoken 替换估算
         AgentContext（修改）              ← 新增 memory_summary 字段
         AgentLoop + main.py（修改）       ← 集成 + /dream 命令
```

---

## 四、各模块开发要点

### 4.1 `memory/storage.py` — MemoryStorage（新建）

**职责**：SQLite + FTS5 双索引 + embedding BLOB 存储 + 文件变更检测。

**核心设计**：

```python
@dataclass
class MemoryChunk:
    id: str
    path: str                    # 来源文件相对路径（相对于 project_root）
    start_line: int
    end_line: int
    text: str
    embedding: list[float] | None
    hash: str
    source: str                  # "memory" | "session" | "knowledge"
    metadata: dict | None = None

@dataclass
class SearchResult:
    path: str
    start_line: int
    end_line: int
    score: float
    snippet: str
    source: str

class MemoryStorage:
    def __init__(self, db_path: Path):
        # SQLite WAL 模式 + 双 FTS5
        ...

    def search_vector(self, query_embedding, limit) -> list[SearchResult]:
        """numpy 向量化余弦相似度，~100x 快于 Python 循环"""
        ...

    def search_keyword(self, query, limit) -> list[SearchResult]:
        """FTS5 + trigram + LIKE 三级降级"""
        ...

    def save_chunks_batch(self, chunks: list[MemoryChunk]): ...
    def delete_by_path(self, path: str): ...
```

**SQLite Schema 完整定义**：

```sql
-- chunks 核心表
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding BLOB,          -- np.float32.tobytes() 二进制，比 JSON 小 6 倍
    hash TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'memory',
    metadata TEXT,
    created_at INTEGER DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER DEFAULT (strftime('%s', 'now'))
);

-- 文件元数据（变更检测核心）
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    hash TEXT NOT NULL,      -- SHA256，文件内容 hash
    mtime INTEGER NOT NULL,  -- 文件修改时间戳
    size INTEGER NOT NULL,   -- 文件字节大小
    updated_at INTEGER DEFAULT (strftime('%s', 'now'))
);

-- FTS5 全文索引（英文）
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, id UNINDEXED, path UNINDEXED, source UNINDEXED,
    content='chunks', content_rowid='rowid'
);

-- FTS5 三字符索引（中文 CJK）
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_trigram USING fts5(
    text, id UNINDEXED, path UNINDEXED, source UNINDEXED,
    content='chunks', content_rowid='rowid',
    tokenize='trigram case_sensitive 0'
);
```

**文件变更检测算法**（`sync()` 的核心逻辑）：

```
对每个被监控的 Markdown 文件：
  1. 计算文件当前 SHA256 hash + mtime + size
  2. 查询 files 表，比较 hash 是否匹配
  3. 不匹配 → 标记为 dirty：
     a. delete_by_path(path) 删除旧 chunks
     b. TextChunker.chunk_text() 重新分块
     c. EmbeddingProvider.embed_batch() 批量生成向量
     d. save_chunks_batch() 批量写入新 chunks
     e. UPSERT files 表更新 hash/mtime/size
  4. 匹配 → 跳过
```

**关键技巧**（从 CowAgent 学到的）：
- Embedding 用 `np.float32.tobytes()` BLOB 存储，比 JSON 小 6 倍
- **UPSERT > INSERT OR REPLACE**：后者改变 rowid，导致 FTS5 content_rowid 漂移
- 自愈机制：FTS5 shadow table 损坏时自动从 chunks 表重建
- CJK 检测：Unicode 范围正则，编译一次模块级复用
- 时间衰减：日记忆文件按半衰期指数衰减，MEMORY.md 条目不衰减（常青）

### 4.2 `memory/chunker.py` — TextChunker（新建）

**职责**：文本分块，Markdown 结构感知。

```python
@dataclass
class TextChunk:
    text: str
    start_line: int
    end_line: int

class TextChunker:
    def __init__(
        self,
        max_tokens: int = 500,      # 每块最大 token 估算
        overlap_tokens: int = 50,   # 块间重叠
    ): ...

    def chunk_text(self, text: str) -> list[TextChunk]:
        """
        按行 + token 估算分块。Markdown 结构感知（不切断 ## 标题边界）。
        分块策略：遇到 ## 标题时优先在此处切分，保证每个 chunk 语义完整。
        """
        ...
```

**为什么自研**：LangChain 的 RecursiveCharacterTextSplitter 引入依赖链太重。按行 + token 估算分块逻辑简单，自研更可控。

### 4.3 `memory/embedding.py` — EmbeddingProvider（新建）

**职责**：向量嵌入抽象，复用 OpenAI-compatible API 模式。

```python
class EmbeddingProvider(ABC):
    @abstractmethod
    def embed_query(self, text: str) -> list[float]: ...
    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible embedding API"""
    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str = "text-embedding-v3",
        dimensions: int = 1024,
    ): ...
    # embed_batch 内部自动分页（max 16 条/次 API 调用）

class EmbeddingCache:
    """会话级嵌入缓存，基于 collections.OrderedDict 实现 LRU 淘汰（max 256 条）。
       键 = 文本内容的 SHA256 前 16 位，值 = embedding list。
       缓存命中时直接从 OrderedDict 返回，不调用 API。"""
    ...
```

**供应商支持**：初期只实现 OpenAI-compatible（覆盖千问 dashscope / OpenAI / 豆包火山引擎），不同供应商只是 api_base 和 model 不同。

**降级策略**：EmbeddingProvider 为 None 时，MemoryManager 降级为纯关键词搜索（FTS5 + trigram）。

### 4.4 `memory/manager.py` — MemoryManager（新建）

**职责**：统一记忆检索入口 + sync 调度 + flush/dream 触发。

```python
class MemoryManager:
    def __init__(
        self,
        config: MemoryConfig,
        storage: MemoryStorage,
        chunker: TextChunker,
        embedding_provider: EmbeddingProvider | None = None,
        flush_manager: MemoryFlushManager | None = None,
    ):
        # embedding_provider 为 None 时降级为纯关键词搜索
        ...

    async def search(
        self, query: str,
        max_results: int = 5,
        min_score: float = 0.1,
    ) -> list[SearchResult]:
        """
        混合检索：
        1. 若 sync_on_search: 先 await sync() 检查文件变更
        2. 向量检索（权重 0.7）：embed query → search_vector()
        3. 关键词检索（权重 0.3）：search_keyword()
        4. 时间衰减：日记忆按半衰期衰减，MEMORY.md 不衰减
        5. 加权合并 + 截断 max_results + min_score 过滤
        """
        ...

    async def sync(self, force: bool = False) -> None:
        """
        文件变更检测 → 分块 → 批量 embedding → 写入索引。
        两阶段：先收集所有 dirty 文件的 chunk，再 batch embed（避免逐条 API 调用）。
        embedding 失败时保留原索引，下次 sync 重试。
        触发时机：
        - sync_on_search=True: 每次 search() 前自动检查（默认）
        - force=True: 强制全量重建（忽略 hash 比对，重新分块所有文件）
        """
        ...

    async def flush_memory(
        self, messages: list[SessionMessage],
        reason: str = "threshold",
    ) -> bool:
        """将对话摘要写入日记忆文件，标记 dirty"""
        ...
```

**核心要点**：
- `search()` 是检索入口——在 `AgentLoop._build_context()` 中被异步调用
- `sync()` 负责文件 → 索引的同步，`sync_on_search: true` 时搜索前自动检查
- `flush_memory()` 触发 L2 写入，在 AgentLoop.run() 正常完成时异步调用
- `MemoryStorage.__init__()` 接收 `db_path: Path`（**已由 MemoryConfig.get_db_path(project_root) 解析为绝对路径**）
- 相对路径基于 `project_root`（`_find_project_root()`）解析——MemoryConfig 的辅助方法接收 `project_root` 参数完成转换

### 4.5 `memory/flush.py` — MemoryFlushManager（新建）

**职责**：将对话摘要写入 `memory/YYYY-MM-DD.md` 日记忆文件。

```python
class MemoryFlushManager:
    def __init__(self, workspace_dir: Path, llm: Any): ...

    async def flush_from_messages(
        self,
        messages: list[SessionMessage],
        reason: str = "threshold",       # "threshold" | "overflow" | "manual"
        max_messages: int = 10,
    ) -> bool:
        """
        1. 取最近 max_messages 条消息（含 user + assistant 完整往返）
        2. 调用 LLM 生成 2-3 句中文摘要
        3. 检查同日已有摘要的 content hash——若相同则跳过（防止同日多次 flush 产生重复摘要）
        4. 追加到 data/memory/YYYY-MM-DD.md，格式: ## {timestamp}\n{summary}\n
        5. 异常静默（asyncio.create_task 后台执行，不抛给调用方）
        """
        ...
```

**与旧版的关键区别**：
- ❌ 旧版：传 `AgentResult.final_text[:500]` — 缺少对话上下文
- ✅ 新版：传最近 N 条完整的 `SessionMessage` 列表 — LLM 能看到 user↔assistant 往返

**去重策略**：基于当日已有摘要的 content SHA256 hash 比对——同一日相同内容的摘要不会重复写入。

**触发时机**（在 AgentLoop 中集成，仅在 run() 正常完成时触发）：

```python
# agent/loop.py — run() 末尾（成功路径，不在 finally 块中）
# flush 仅在 run() 正常完成时触发，异常退出不写入日记忆（避免写入脏数据）
async def run(self, user_message: str) -> AgentResult:
    ...
    # 正常完成路径
    if self._memory_mgr and len(self.session.messages) > self.config.memory.flush_threshold:
        asyncio.create_task(
            self._memory_mgr.flush_memory(
                messages=self.session.messages[-self.config.memory.flush_max_messages:],
                reason="threshold"
            )
        )
    return result
```

### 4.6 `memory/dream.py` — DeepDream（新建）

**职责**：L3 蒸馏——将日记忆蒸馏为 MEMORY.md 长期记忆。

```python
class DeepDream:
    def __init__(self, workspace_dir: Path, llm: Any): ...

    async def run(self, force: bool = False) -> str:
        """
        1. 读取所有日记忆文件 (data/memory/YYYY-MM-DD.md)
        2. 读取当前 MEMORY.md
        3. 调用 LLM：合并 + 语义去重 + 提炼关键信息
        4. 写入 MEMORY.md（覆盖模式）
        5. 更新 .dream_state.json 元数据
        6. 返回蒸馏摘要："已蒸馏 {N} 日，新增 {M} 条记忆"
        """
        ...
```

**蒸馏 Prompt 设计**：
- system_prompt: `"你是记忆提炼助手。阅读以下对话摘要和已有长期记忆，合并提炼为简洁的 Markdown 列表。要求：1) 提取用户偏好、重要决策、待办事项、学到的知识 2) 与已有记忆语义相近的条目合并而非新增 3) 忽略闲聊和重复信息 4) 每行格式：'- [日期] 内容'"`
- user_message：当日 memory 文件全文 + MEMORY.md 现有内容

**蒸馏状态管理**：使用独立元数据文件 `data/memory/.dream_state.json`（替代旧版文件末尾的 `--- distilled` 标记行）：

```json
{
  "2026-05-30": {"distilled_at": "2026-05-31T23:55:00", "entries": 12, "hash": "abc123"},
  "2026-05-31": {"distilled_at": null}
}
```

**触发方式**（三种，可组合）：

| 方式 | 实现 | 触发条件 | P4 状态 |
|------|------|----------|---------|
| 手动 | `/dream` CLI 命令 | 用户主动触发 | ✅ 实现 |
| 定时 | Scheduler cron（P8 完善） | 每天 23:55 | ⏳ 字段预留（`dream_schedule`），P8 启用 |
| 自动 | AgentLoop 会话结束时 | 自动触发 | ⏳ 接口预留，暂不实现 |

**去重策略**（两层）：
1. **SHA256 行级去重**：防止完全相同条目重复写入
2. **LLM 语义合并**：distillation prompt 明确要求与已有记忆语义相近的条目合并

### 4.7 `agent/prompt/providers.py` — MemoryProvider 激活（修改）

**当前**：`MemoryProvider.provide()` 返回 `None`（P3 骨架）

**P4 版本**——单一数据通路，从 `AgentContext.memory_summary` 读取：

```python
class MemoryProvider(DataProvider):
    """
    记忆上下文注入。
    数据来源：AgentContext.memory_summary（由 AgentLoop._build_context() 异步填充）。
    MemoryProvider 保持纯同步，符合 DataProvider 接口约定。
    """
    def __init__(self, memory_manager: MemoryManager):
        self._memory_mgr = memory_manager

    @property
    def section_name(self) -> str:
        return "memory"

    def provide(self, context: AgentContext) -> str | None:
        # 从 context.memory_summary 读取（_build_context 中已做语义检索并填充）
        if not context.memory_summary:
            return None
        return f"## 相关记忆\n\n{context.memory_summary}"
```

**设计决策**：
- ❌ 不提供 `provide_async()` 方法——`PromptBuilder.build()` 是同步方法，永远不会调用 async provider
- ✅ 异步语义检索在 `AgentLoop._build_context()` 中完成（见 §4.10），结果存入 `AgentContext.memory_summary`
- ✅ `MemoryProvider.provide()` 只做"从 context 读取 → 格式化 → 返回"，简单可测
- ✅ EmbeddingProvider 未配置时：`_build_context()` 降级读取 MEMORY.md 全文作为 memory_summary，MemoryProvider 无感知

### 4.8 `agent/context.py` — AgentContext 修改

**修改范围**：
- dataclass 新增字段：`memory_summary: str = ""`（由 `_build_context()` 填充）
- 其他字段不变（保持 frozen=True）

### 4.9 `agent/message_utils.py` — Token 升级

**修改范围**：
- 删除 P3 的 `_estimate_tokens()` 中英文差异化估算函数
- 新增模块级变量：`_ENCODER = tiktoken.get_encoding("cl100k_base")`（懒加载，首次调用时初始化）
- `trim()` 中所有 token 估算改为 `len(_ENCODER.encode(content))`

**关于 `cl100k_base` 对中文文本的近似性**：
- `cl100k_base` 是 OpenAI GPT-4 的 tokenizer，Qwen/DeepSeek 使用自研 tokenizer（词表不同）
- 对纯英文文本，偏差约 5-10%；对中文文本（dotClaw 主要使用场景），偏差约 15-30%
- **影响可控**：P3 中 `trim()` 已预留 20% 安全边界（`target_tokens * 0.8`），实际裁剪不会因为偏差导致超限
- 后续可考虑根据当前使用的 model 动态选择编码器
- 后续新增不兼容供应商（如 Gemini）时需支持多编码器

### 4.10 `agent/loop.py` — 修改

**要点**：
- `__init__` 新增 `memory_mgr: MemoryManager | None = None`
- **`_build_context()` 从同步方法改为 `async def`**——原因：需要 `await self._memory_mgr.search(user_message)` 做语义检索

```python
# P3（当前）
context = self._build_context(user_message)         # 同步

# P4（改为）
context = await self._build_context(user_message)   # async

async def _build_context(self, user_message: str) -> AgentContext:
    memory_summary = ""
    if self._memory_mgr:
        results = await self._memory_mgr.search(user_message, max_results=3)
        if results:
            memory_summary = "\n".join(
                f"- ({r.source}:{r.path}) {r.snippet}" for r in results
            )
    return AgentContext(..., memory_summary=memory_summary)
```

- `run()` 中 `_build_context()` 的调用改为 `await`——`run()` 本身就是 `async def`，无需额外重构
- `run()` 末尾（成功路径，不在 finally 块）检查 flush_threshold，触发 `flush_memory()`
- **不通过 `_last_context` 私有属性**——flush 的信息直接从 `self.session.messages` 获取

**P3 回归测试兼容**：P3 测试中 Mock 了 `_build_context()` 返回值的场景需要从 `Mock` 改为 `AsyncMock`。影响范围小，仅 2-3 个测试用例。

### 4.11 `main.py` — 修改

**要点**：
- 初始化 MemoryStorage → TextChunker → EmbeddingProvider → MemoryManager → MemoryFlushManager → DeepDream 完整依赖链
- 传入 `AgentLoop(..., memory_mgr=memory_mgr)`
- 新增 `/dream` 命令处理（调用 `DeepDream.run()`）
- 旧版 `asyncio.create_task(memory_flush.summarize_async(...))` 的手动调用已删除——flush 由 AgentLoop 内部 `run()` 末尾自动触发

### 4.12 `config/settings.py` — 配置扩展

**MemoryConfig 完全定义在 `config/settings.py`**，不创建独立的 `memory/config.py`。与现有 `MemoryConfig(long_term_file=...)` 合并扩展。

```python
# config/settings.py
@dataclass
class MemoryConfig:
    # P1 已有字段
    long_term_file: str = "./data/memory/MEMORY.md"

    # P4 新增字段
    workspace: str = "./data"
    db_path: str = "./data/memory/memory.db"
    chunk_max_tokens: int = 500
    chunk_overlap_tokens: int = 50
    embedding_provider: str | None = None
    embedding_model: str = "text-embedding-v3"
    embedding_dimensions: int = 1024
    embedding_api_base: str = ""        # 留空跟随 llm.api_base
    embedding_api_key: str = ""         # 留空跟随 llm.api_key
    max_results: int = 5
    min_score: float = 0.1
    vector_weight: float = 0.7
    keyword_weight: float = 0.3
    sync_on_search: bool = True
    flush_threshold: int = 20           # 消息数超此值触发 flush
    flush_max_messages: int = 10        # flush 时取最近 N 条
    dream_enabled: bool = True
    dream_schedule: str = "55 23 * * *" # P8 Scheduler 启用，P4 仅定义字段不消费
    temporal_decay_half_life_days: float = 30.0

    # 辅助方法：将相对路径转为基于 project_root 的绝对路径
    def get_db_path(self, project_root: Path) -> Path:
        return _resolve_path(self.db_path, project_root)

    def get_memory_dir(self, project_root: Path) -> Path:
        return _resolve_path(self.workspace, project_root) / "memory"

    def get_workspace(self, project_root: Path) -> Path:
        return _resolve_path(self.workspace, project_root)

def _resolve_path(path_str: str, project_root: Path) -> Path:
    """将相对路径基于 project_root 解析为绝对路径"""
    p = Path(path_str)
    if p.is_absolute():
        return p
    return project_root / p
```

**路径解析规则**：
- MemoryConfig 中的所有相对路径（`workspace`、`db_path`、`long_term_file`）基于 `project_root`（`_find_project_root()`）解析
- `MemoryStorage.__init__()` 接收的是**已解析为绝对路径**的 `db_path`（通过 `MemoryConfig.get_db_path(project_root)` 转换）
- `MemoryFlushManager.__init__()` 接收的是**已解析为绝对路径**的 `workspace_dir`
- 与 `_find_project_root()` 使用同一个 project_root，保证一致性

---

## 五、开发实施顺序

```
Step 1: memory/storage.py          ← 基础层：SQLite 建表 + FTS5 双索引
Step 2: memory/chunker.py           ← 文本分块，Markdown 结构感知
Step 3: memory/embedding.py         ← EmbeddingProvider + LRU Cache
Step 4: memory/manager.py           ← MemoryManager 核心调度：search() + sync() + flush 触发
Step 5: memory/flush.py             ← MemoryFlushManager：L2 日记忆写入（含同日去重）
Step 6: memory/dream.py             ← DeepDream：L3 蒸馏 + .dream_state.json
Step 7: config/settings.py           ← MemoryConfig 扩展（合并已有字段，新增 P4 字段）
Step 8: agent/context.py             ← 新增 memory_summary 字段
Step 9: agent/message_utils.py      ← tiktoken 替换估算公式
Step 10: agent/prompt/providers.py  ← MemoryProvider 激活（读取 context.memory_summary）
Step 11: agent/loop.py              ← memory_mgr 集成 + _build_context 改 async + flush 触发
Step 12: main.py                    ← 初始化依赖链 + /dream 命令
Step 13: pyproject.toml             ← 新增 tiktoken + numpy 依赖
Step 14: tests/test_phase4_acceptance.py  ← 自动化测试（7 个场景，详见 §8）
Step 15: 回归验收
```

Step 1-3 可并行（互相独立的基础层），Step 4 依赖 Step 1+2+3，Step 5+6 依赖 Step 4。

---

## 六、新增依赖

```toml
# pyproject.toml [dependencies]
dependencies = [
    # 已有
    "openai>=1.30.0",
    "pyyaml>=6.0",
    "aiofiles>=23.0",
    "rich>=13.0",
    # Phase 4 新增
    "tiktoken>=0.7.0",     # Token 精确计算（cl100k_base）
    "numpy>=1.26.0",       # 向量计算 + embedding BLOB 编解码
]

# numpy 可选降级：缺失时 MemoryStorage.search_vector() 降级为纯 Python 余弦相似度
# 但推荐安装（~100x 性能差距）
```

**不引入的库**：
- LangChain / LangGraph：与项目原则冲突
- ChromaDB / FAISS：SQLite + numpy 已够用，且理解向量检索原理更有学习价值
- Mem0：过度封装

---

## 七、目录结构变化

```
src/dotclaw/memory/
├── __init__.py           # 导出 MemoryManager
├── store.py              # SessionManager（L1，已有，不改）
├── storage.py            # MemoryStorage（新建，SQLite + FTS5 + 向量）
├── chunker.py            # TextChunker（新建，文本分块）
├── embedding.py          # EmbeddingProvider + EmbeddingCache（新建）
├── manager.py            # MemoryManager（新建，核心调度）
├── flush.py              # MemoryFlushManager（新建，L2 flush）
└── dream.py              # DeepDream（新建，L3 蒸馏）
# MemoryConfig 不在 memory/ 目录下，统一在 config/settings.py

data/memory/
├── memory.db             # SQLite 数据库
├── MEMORY.md             # 长期记忆
├── YYYY-MM-DD.md         # 日记忆文件
└── .dream_state.json     # 蒸馏状态元数据
```

---

## 八、自动化测试计划

新增 `tests/test_phase4_acceptance.py`，覆盖 7 个场景。利用 P1-P3 已有的 `tempfile.TemporaryDirectory` 模式做隔离 SQLite 测试。

| # | 测试场景 | 验证内容 |
|---|---------|---------|
| 1 | MemoryStorage CRUD + 关键词搜索 | FTS5 中文 trigram 搜索、英文 unicode61 搜索、UPSERT rowid 稳定性 |
| 2 | MemoryStorage 向量检索 | numpy 余弦相似度计算、embedding BLOB round-trip、numpy 缺失降级 |
| 3 | TextChunker Markdown 分块 | 不切断 `##` 标题边界、重叠段正确、块大小在 max_tokens 范围内 |
| 4 | MemoryManager 混合检索 | 向量+FTS5 加权融合（0.7/0.3）、时间衰减计算、min_score 过滤 |
| 5 | MemoryManager Embedding 降级 | `embedding_provider=None` 时降级为纯关键词、不抛异常 |
| 6 | DeepDream 去重 + 状态管理 | SHA256 行级去重、`.dream_state.json` 状态正确、force=True 重蒸馏 |
| 7 | MemoryProvider 注入 | `memory_summary` 为空时返回 None、非空时正确格式化注入 |

---

## 九、验收标准

### 9.1 功能验收

**场景 1：基础存储 + 关键词检索**
- 手动创建 `data/memory/MEMORY.md`，写入内容
- 启动 dotclaw，MemoryStorage 初始化后 sync 将文件分块写入 SQLite
- 运行 `search_keyword("API 设计")` 返回相关 chunk
- 预期：中文 trigram 搜索正常，英文 unicode61 搜索正常

**场景 2：混合检索（向量 + FTS5）**
- 配置 embedding_provider 后，sync 生成 embedding 并存储
- 搜索 `"昨天讨论的模型路由方案"` 返回语义相关结果
- 预期：向量检索结果排在关键词结果之前（向量权重 0.7）

**场景 3：日记忆自动 flush**
- 连续对话超过 `flush_threshold`（默认 20 条消息）
- 预期：`data/memory/YYYY-MM-DD.md` 自动出现，包含对话摘要
- CLI 无额外等待延迟（asyncio.create_task 异步）
- 异常退出不写入日记忆

**场景 4：Deep Dream 蒸馏**
- 完成几天对话后，输入 `/dream`
- 预期：输出 `已蒸馏 N 日，新增 M 条记忆`
- `data/memory/MEMORY.md` 出现蒸馏后的长期记忆
- `data/memory/.dream_state.json` 记录蒸馏状态

**场景 5：去重保护**
- `/dream` 执行两次
- 预期：第二次显示"新增 0 条记忆"（已蒸馏文件不重复处理）
- 语义相近的条目被 LLM 合并，不产生冗余行

**场景 6：记忆注入 system prompt**
- MEMORY.md 中有记忆条目后，发起相关对话
- 预期：system prompt 中出现"## 相关记忆" section
- LLM 的回答能引用记忆内容

**场景 7：Embedding 降级**
- 不配置 embedding_provider（或配置无效 endpoint）
- 预期：MemoryManager 降级为纯关键词搜索，不抛异常
- 搜索功能仍可用（FTS5 + trigram）

**场景 8：flush 失败不影响对话**
- 手动配置无效 LLM endpoint 给 MemoryFlushManager
- 完成对话
- 预期：CLI 正常返回，后台 flush 静默失败（日志有记录）

### 9.2 Token 验收

**场景 9：Token 计数准确性**
- 验证 `len(tiktoken.get_encoding("cl100k_base").encode("你好世界"))` 返回合理数值
- 运行带工具调用的对话，验证 `message_utils.trim()` 使用新编码器后的裁剪行为
- 中文文本 token 计数与原始 P3 估算公式对比（cl100k_base 值偏大约 15-30%，安全边界覆盖）

### 9.3 回归验收

- P1 的 5 个验收场景全部通过
- P2 的模型切换和降级功能正常
- P3 的 6 个功能场景全部通过（含 `memory_summary` 为空时的兼容性验证）
- `_build_context()` 异步化后，P3 测试中 Mock 的同步调用改为 `AsyncMock`
- `tests/test_phase1_acceptance.py` 全部通过
- `tests/test_phase2_acceptance.py` 全部通过
- `tests/test_phase3_acceptance.py` 全部通过
- `tests/test_phase4_acceptance.py` 全部通过（7 个场景）

### 9.4 代码质量

- MemoryStorage 对 FTS5 shadow table 损坏有自愈机制
- `save_chunks_batch()` 使用 UPSERT 而非 INSERT OR REPLACE（保证 FTS5 rowid 稳定）
- MemoryFlushManager 的 LLM 调用失败有 try/except，不影响主流程
- `sync()` embedding 失败时保留原索引，下次 sync 重试
- `search_vector()` 在 numpy 缺失时降级为纯 Python 计算
- `MemoryProvider.provide()` 纯同步，不引入异步依赖

---

## 十、文件清单

| 文件 | 状态 | 复杂度 | 说明 |
|------|------|--------|------|
| `memory/storage.py` | 新建 | 高 | SQLite 建表、双 FTS5、向量检索、files 表变更检测 |
| `memory/chunker.py` | 新建 | 低 | 按行 + token 估算分块，Markdown 感知 |
| `memory/embedding.py` | 新建 | 中 | EmbeddingProvider + LRU EmbeddingCache（OrderedDict） |
| `memory/manager.py` | 新建 | 高 | 核心调度：混合检索 + sync + flush 触发 |
| `memory/flush.py` | 新建 | 中 | L2 日记忆写入（含同日 content hash 去重） |
| `memory/dream.py` | 新建 | 中 | L3 Deep Dream 蒸馏 + .dream_state.json |
| `agent/context.py` | 修改 | 低 | 新增 memory_summary 字段 |
| `agent/prompt/providers.py` | 修改 | 低 | MemoryProvider 激活（读 context.memory_summary） |
| `agent/message_utils.py` | 修改 | 中 | tiktoken 替换估算（cl100k_base） |
| `agent/loop.py` | 修改 | 中 | memory_mgr 集成 + _build_context 改 async + flush 触发 |
| `main.py` | 修改 | 中 | 初始化依赖链 + /dream 命令 |
| `config/settings.py` | 修改 | 中 | MemoryConfig 扩展（合并已有字段，新增 P4 字段） |
| `config.yaml` | 修改 | 低 | 新增 memory 段 |
| `pyproject.toml` | 修改 | 低 | 新增 tiktoken + numpy 依赖 |
| `tests/test_phase4_acceptance.py` | 新建 | 中 | 7 个自动化测试场景 |

**注意**：不创建 `memory/config.py`——MemoryConfig 完全定义在 `config/settings.py`，与项目现有约定一致（所有配置 dataclass 集中管理）。

---

## 十一、开发注意事项

1. **P3 回归优先**：P4 激活 MemoryProvider 后，P3 测试中 `memory_summary` 为空时对话行为不变
2. **MemoryConfig 单一位置**：所有 MemoryConfig 字段在 `config/settings.py`，不创建 `memory/config.py`
3. **单一数据通路**：`_build_context()` 做语义检索 → `AgentContext.memory_summary` → `MemoryProvider.provide()` 读取——不存在 `provide_async()`，不存在直接读 MEMORY.md 的路径
4. **`_build_context()` 异步化**：从同步改为 `async def`，P3 测试中 Mock 需从 `Mock` → `AsyncMock`
5. **EmbeddingProvider 可空**：`None` 时降级为纯关键词搜索，保证最小可用
6. **UPSERT 而非 INSERT OR REPLACE**：后者改变 rowid 导致 FTS5 content_rowid 漂移——这是 CowAgent 踩过的坑
7. **sync 两阶段**：先收集所有 chunk，再 batch embed——逐条 API 调用的延迟不可接受
8. **`.dream_state.json` 独立元数据**：不用文件末尾 `--- distilled` 标记行——文件被用户手动编辑后也不会丢失蒸馏状态
9. **flush 输入是 messages 列表**：取最近 N 条完整消息（含 user + assistant 往返），不是 result.final_text 片段
10. **flush 仅在成功路径触发**：`run()` 正常完成时写入日记忆，异常退出不写入（避免脏数据）
11. **flush 同日去重**：同日相同 content hash 的摘要不重复写入
12. **cl100k_base 中文偏差**：对中文文本有 15-30% 的 token 计数偏差，P3 的 20% 安全边界覆盖此偏差
13. **numpy 可选**：缺失时向量检索降级为纯 Python，但打印性能警告
14. **时间衰减**：仅对日记忆文件生效（30 天半衰期），MEMORY.md 蒸馏条目不衰减（常青）
15. **flush_threshold 默认 20**：基于消息条数触发，非基于字符数——更直观且配置简单
16. **dream_schedule P8 启用**：P4 仅定义字段和默认值，cron 调度逻辑由 P8 Scheduler 消费
17. **路径解析**：MemoryConfig 中的相对路径基于 `project_root`（`_find_project_root()`），通过 `get_db_path(project_root)` 等辅助方法转为绝对路径后传入各模块
