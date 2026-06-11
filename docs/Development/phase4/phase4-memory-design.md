# dotClaw Phase 4：三级记忆系统设计文档

> 基于 CowAgent 记忆系统源码分析，结合 dotClaw 已有架构设计

## 1. 设计目标

在 dotClaw 已有的 AgentContext + PromptBuilder + SessionManager 基础上，构建三级记忆架构：

| 层级 | 名称 | 存储 | 生命周期 | 作用 |
|------|------|------|----------|------|
| L1 | 工作记忆 | SessionManager (JSON) | 单次会话 | 当前对话上下文 |
| L2 | 日记忆 | `memory/YYYY-MM-DD.md` | 天级 | 当日对话摘要 + 关键决策 |
| L3 | 长期记忆 | `MEMORY.md` + 向量索引 | 永久 | 蒸馏后的核心知识 |

**核心原则**：核心模块自研，基础设施用库。记忆检索逻辑、三级流转、蒸馏触发全部自研；向量存储、Embedding 计算用成熟库。

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────┐
│                      AgentLoop                          │
│  run() → AgentContext → PromptBuilder.build() → LLM    │
└──────────────┬──────────────────────┬───────────────────┘
               │                      │
               ▼                      ▼
     ┌─────────────────┐   ┌──────────────────┐
     │  MemoryProvider  │   │  MemoryManager   │
     │  (PromptBuilder) │   │  (核心调度)       │
     └────────┬────────┘   └──┬───┬───┬───────┘
              │               │   │   │
              ▼               ▼   ▼   ▼
    ┌──────────────┐   ┌────┐ ┌──┐ ┌──────────┐
    │ Prompt 注入   │   │L2  │ │L3│ │ DeepDream│
    │ memory section│   │Flush│ │Distill│ │ 触发器   │
    └──────────────┘   └────┘ └──┘ └──────────┘
                            │        │
                            ▼        ▼
                   ┌──────────────────────┐
                   │   MemoryStorage      │
                   │   (SQLite + FTS5)    │
                   │   + 向量索引          │
                   └──────────────────────┘
```

---

## 3. 模块设计

### 3.1 MemoryManager — 记忆核心调度器

**文件**：`memory/manager.py`（新建）

**职责**：
- 统一记忆检索入口（hybrid search：向量 + 关键词）
- L2 flush 触发（对话溢出时摘要写入日记忆）
- L3 蒸馏触发（Deep Dream）
- 记忆文件同步（文件变更 → 向量索引更新）

**关键接口**：

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
        """混合检索：向量 + FTS5 关键词 + 时间衰减"""
        ...

    async def sync(self, force: bool = False) -> None:
        """文件变更检测 → 分块 → 批量 embedding → 写入索引"""
        ...

    async def flush_memory(
        self, messages: list[SessionMessage],
        reason: str = "threshold",
    ) -> bool:
        """将对话摘要写入日记忆文件，标记 dirty"""
        ...
```

**与 CowAgent 的关键差异**：

| 方面 | CowAgent | dotClaw |
|------|----------|---------|
| 用户隔离 | 多 user_id + scope 体系 | 单用户，去掉 scope/user_id |
| 嵌入提供者 | 构造函数内回退初始化 | 明确传入，None = 关键词降级 |
| 同步触发 | `sync_on_search` 配置 | 同理，搜索前检查 dirty |
| Dream 日记 | 独立 `dreams/` 目录 | 简化：直接蒸馏到 MEMORY.md |

### 3.2 MemoryStorage — 存储层

**文件**：`memory/storage.py`（改造现有，增加 FTS5 + 向量）

**当前状态**：`memory/store.py` 只有 `SessionManager`（JSON 文件读写）

**改造方案**：保留 `SessionManager`（L1 工作记忆不动），新建 `MemoryStorage` 类

**核心设计**（借鉴 CowAgent，简化掉多用户）：

```python
@dataclass
class MemoryChunk:
    id: str
    path: str                    # 来源文件相对路径
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
    """SQLite + FTS5 + 向量检索"""
    
    def __init__(self, db_path: Path):
        # SQLite WAL 模式 + FTS5
        # 双 FTS5：unicode61 (英文) + trigram (中文)
        ...

    def search_vector(self, query_embedding, limit) -> list[SearchResult]:
        """numpy 向量化余弦相似度，~100x 快于 Python 循环"""
        ...

    def search_keyword(self, query, limit) -> list[SearchResult]:
        """FTS5 + LIKE 三级降级"""
        # 1. unicode61 FTS5 (纯英文)
        # 2. trigram FTS5 (中文/混合)
        # 3. LIKE fallback (短 CJK / FTS5 不可用)
        ...

    def save_chunks_batch(self, chunks: list[MemoryChunk]): ...
    def delete_by_path(self, path: str): ...
```

**SQLite Schema**：

```sql
-- 核心表
CREATE TABLE chunks (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding BLOB,          -- float32 二进制，6x 小于 JSON
    hash TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'memory',
    metadata TEXT,
    created_at INTEGER DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER DEFAULT (strftime('%s', 'now'))
);

-- 文件元数据（变更检测）
CREATE TABLE files (
    path TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    mtime INTEGER NOT NULL,
    size INTEGER NOT NULL,
    updated_at INTEGER DEFAULT (strftime('%s', 'now'))
);

-- FTS5 全文索引（英文）
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text, id UNINDEXED, path UNINDEXED, source UNINDEXED,
    content='chunks', content_rowid='rowid'
);

-- FTS5 三字符索引（中文 CJK）
CREATE VIRTUAL TABLE chunks_fts_trigram USING fts5(
    text, id UNINDEXED, path UNINDEXED, source UNINDEXED,
    content='chunks', content_rowid='rowid',
    tokenize='trigram case_sensitive 0'
);
```

**从 CowAgent 学到的关键技巧**：

1. **UPSERT > INSERT OR REPLACE**：后者会改变 rowid，导致 FTS5 content_rowid 漂移
2. **双 FTS5 策略**：unicode61 对中文逐字拆分无意义，trigram 三字符滑窗才是中文正道
3. **Embedding BLOB 存储**：`np.float32.tobytes()` 比 JSON 小 6 倍，读取快
4. **自愈机制**：FTS5 shadow table 损坏时自动从 chunks 重建
5. **CJK 检测**：Unicode 范围正则，编译一次模块级复用
6. **时间衰减**：日记忆文件按日期指数衰减，MEMORY.md 不衰减（常青）

### 3.3 TextChunker — 文本分块

**文件**：`memory/chunker.py`（新建）

**设计**：

```python
@dataclass
class TextChunk:
    text: str
    start_line: int
    end_line: int

class TextChunker:
    def __init__(
        self,
        max_tokens: int = 500,     # 每块最大 token 估算
        overlap_tokens: int = 50,  # 块间重叠
    ): ...

    def chunk_text(self, text: str) -> list[TextChunk]:
        """
        按行数 + token 估算分块。
        Markdown 结构感知（## 标题不切断）。
        """
        ...
```

**为什么自研**：CowAgent 的 chunker 也很简单（按行 + token 估算），不需要引入 LangChain 的 RecursiveCharacterTextSplitter。理解分块原理比调 API 重要。

### 3.4 EmbeddingProvider — 向量嵌入

**文件**：`memory/embedding.py`（新建）

**设计**：

```python
class EmbeddingProvider(ABC):
    """嵌入提供者抽象基类"""

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

    def embed_query(self, text: str) -> list[float]:
        # 单条查询嵌入
        ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        # 批量嵌入，自动分页（max_batch_size=16）
        ...


class EmbeddingCache:
    """会话级嵌入缓存，避免重复 API 调用"""
    ...
```

**供应商支持**（初期只实现 OpenAI-compatible）：

| 供应商 | API 端点 | 维度 |
|--------|----------|------|
| 通义千问 | dashscope API | 1024 |
| OpenAI | api.openai.com | 1536 |
| 豆包 | 火山引擎 | 1024 |

因为 dotClaw 已有 `OpenAICompatibleClient` 基类，embedding 也可以复用同一模式——不同供应商只是 `api_base` 和 `model` 不同。

### 3.5 MemoryFlushManager — L2 日记忆写入

**文件**：`memory/flush.py`（新建）

**设计**：

```python
class MemoryFlushManager:
    """将对话摘要写入日记忆文件"""

    def __init__(
        self,
        workspace_dir: Path,
        llm_model: Any,           # 用于摘要的 LLM
    ): ...

    def flush_from_messages(
        self,
        messages: list[SessionMessage],
        reason: str = "threshold",  # "threshold" | "overflow" | "manual"
        max_messages: int = 10,
        context_summary_callback: Callable[[str], None] | None = None,
    ) -> bool:
        """
        1. 取最近 max_messages 条消息
        2. 调用 LLM 生成摘要
        3. 追加到 memory/YYYY-MM-DD.md
        4. 去重：基于内容 hash 避免重复写入
        5. 异步执行（不阻塞主回复）
        """
        ...
```

**触发时机**（在 AgentLoop 中集成）：

```python
# agent/loop.py — run() 末尾添加
async def run(self, user_message: str) -> AgentResult:
    ...
    # 保存会话后，检查是否需要 flush
    if self._memory_mgr and len(self.session.messages) > self.config.agent.flush_threshold:
        self._memory_mgr.flush_memory(
            messages=self.session.messages,
            reason="threshold"
        )
    ...
```

### 3.6 Deep Dream — L3 蒸馏

**文件**：`memory/dream.py`（新建）

**设计**：

```python
class DeepDream:
    """将日记忆蒸馏为 MEMORY.md 长期记忆"""

    def __init__(
        self,
        workspace_dir: Path,
        llm_model: Any,
    ): ...

    async def run(self) -> str:
        """
        1. 读取所有日记忆文件 (memory/YYYY-MM-DD.md)
        2. 读取当前 MEMORY.md
        3. 调用 LLM：合并 + 去重 + 提炼
        4. 写入 MEMORY.md（覆盖模式）
        5. 返回蒸馏摘要
        """
        ...
```

**触发机制**（三种，可组合）：

| 方式 | 实现 | 触发条件 |
|------|------|----------|
| 定时 | `ReminderManager` / CLI 定时 | 每天 23:55 |
| 手动 | `/dream` CLI 命令 | 用户主动触发 |
| 自动 | AgentLoop 会话结束时 | `session.end` 事件 |

**初版实现**：先做 `/dream` 手动命令 + 定时（复用已有 ReminderManager），自动触发放到 Phase 5。

### 3.7 MemoryProvider — Prompt 注入

**文件**：`agent/prompt/providers.py`（修改已有骨架）

**当前**：`MemoryProvider.provide()` 返回 `None`（P4 预留）

**实现**：

```python
class MemoryProvider(DataProvider):
    """记忆上下文注入"""

    def __init__(self, memory_manager: MemoryManager):
        self._memory_mgr = memory_manager

    @property
    def section_name(self) -> str:
        return "memory"

    def provide(self, context: AgentContext) -> str | None:
        # 同步方式：直接读文件内容
        # 异步方式（后续）：在 AgentLoop.run() 中预加载
        memory_path = context.workspace / "data" / "memory" / "MEMORY.md"
        if not memory_path.exists():
            return None
        content = memory_path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        return f"## 长期记忆\n\n{content}"
```

**进阶**（向量检索激活后）：

```python
    async def provide_async(self, context: AgentContext, query: str) -> str | None:
        """基于用户消息做语义检索，注入最相关的记忆片段"""
        results = await self._memory_mgr.search(query, max_results=3)
        if not results:
            return None
        lines = ["## 相关记忆\n"]
        for r in results:
            lines.append(f"- ({r.path}) {r.snippet}")
        return "\n".join(lines)
```

---

## 4. 配置设计

**在 `config.yaml` 中新增**：

```yaml
memory:
  # 基础设置
  workspace: "./data"                # 工作空间根目录
  db_path: "./data/memory/memory.db" # SQLite 数据库路径

  # 分块
  chunk_max_tokens: 500
  chunk_overlap_tokens: 50

  # Embedding
  embedding_provider: "openai"       # "openai" | "dashscope" | null(关键词模式)
  embedding_model: "text-embedding-v3"
  embedding_dimensions: 1024
  embedding_api_base: ""             # 留空则跟随 llm.api_base
  embedding_api_key: ""              # 留空则跟随 llm.api_key

  # 检索
  max_results: 5
  min_score: 0.1
  vector_weight: 0.7
  keyword_weight: 0.3
  sync_on_search: true

  # L2 Flush
  flush_threshold: 20                # 消息数超过此值触发 flush
  flush_max_messages: 10             # flush 时取最近 N 条

  # L3 Deep Dream
  dream_enabled: true
  dream_schedule: "55 23 * * *"      # cron 表达式

  # 时间衰减
  temporal_decay_half_life_days: 30
```

**Config dataclass 扩展**：

```python
# config/settings.py
@dataclass
class MemoryConfig:
    workspace: str = "./data"
    db_path: str = "./data/memory/memory.db"
    chunk_max_tokens: int = 500
    chunk_overlap_tokens: int = 50
    embedding_provider: str | None = None
    embedding_model: str = "text-embedding-v3"
    embedding_dimensions: int = 1024
    embedding_api_base: str = ""
    embedding_api_key: str = ""
    max_results: int = 5
    min_score: float = 0.1
    vector_weight: float = 0.7
    keyword_weight: float = 0.3
    sync_on_search: bool = True
    flush_threshold: int = 20
    flush_max_messages: int = 10
    dream_enabled: bool = True
    dream_schedule: str = "55 23 * * *"
    temporal_decay_half_life_days: float = 30.0

    def get_db_path(self) -> Path:
        return _resolve_data_dir(self.db_path)

    def get_memory_dir(self) -> Path:
        return _resolve_data_dir(self.workspace) / "memory"

    def get_workspace(self) -> Path:
        return _resolve_data_dir(self.workspace)
```

---

## 5. 新增依赖

```toml
# pyproject.toml [dependencies]
dependencies = [
    # 已有
    "openai>=1.30.0",
    "pyyaml>=6.0",
    "aiofiles>=23.0",
    "rich>=13.0",
    # Phase 4 新增
    "numpy>=1.26.0",      # 向量计算 + embedding BLOB 编解码
]

# numpy 是可选的：缺失时降级为纯 Python 余弦相似度 + struct.pack
# 但推荐安装（~100x 性能差距）
```

**不引入的库**：
- ❌ LangChain / LangGraph：与项目原则冲突（避免配置文件工程）
- ❌ ChromaDB / FAISS：SQLite + numpy 已够用，且理解向量检索原理更有价值
- ❌ Mem0：过度封装，且 dotClaw 需要自研理解原理

---

## 6. 目录结构变化

```
src/dotclaw/memory/
├── __init__.py           # 导出 MemoryManager
├── store.py              # SessionManager（L1，已有，不改）
├── manager.py            # MemoryManager（新建，核心调度）
├── storage.py            # MemoryStorage（新建，SQLite+FTS5+向量）
├── chunker.py            # TextChunker（新建，文本分块）
├── embedding.py          # EmbeddingProvider + Cache（新建）
├── flush.py              # MemoryFlushManager（新建，L2 flush）
├── dream.py              # DeepDream（新建，L3 蒸馏）
└── config.py             # MemoryConfig（新建，从 settings.py 分离）
```

---

## 7. 实现路线图

### Step 1：MemoryStorage + TextChunker（基础层）

**目标**：SQLite 建表 + FTS5 索引 + 文本分块

- [ ] 新建 `memory/storage.py`：SQLite 初始化、chunks 表、双 FTS5
- [ ] 新建 `memory/chunker.py`：按行 + token 估算分块
- [ ] 单元测试：分块正确性、FTS5 中文检索、UPSERT rowid 不变

**验收**：纯关键词检索可用，中文 trigram 搜索正常

### Step 2：EmbeddingProvider + 向量检索

**目标**：接入 embedding API，实现混合检索

- [ ] 新建 `memory/embedding.py`：OpenAIEmbeddingProvider + EmbeddingCache
- [ ] MemoryStorage 增加 `search_vector()`
- [ ] MemoryManager.search() 实现混合检索 + 时间衰减
- [ ] 单元测试：embedding 批量、余弦相似度、混合排序

**验收**：`memory_manager.search("昨天讨论的API设计")` 返回相关记忆

### Step 3：MemoryManager + 同步

**目标**：文件变更检测 → 分块 → 批量 embedding → 写索引

- [ ] 新建 `memory/manager.py`：MemoryManager 核心逻辑
- [ ] 实现两阶段 sync：先 chunk 收集，再 batch embed
- [ ] 单元测试：增量同步、全量同步、embedding 失败回退

**验收**：修改 MEMORY.md 后 search 能找到新内容

### Step 4：L2 Flush + L3 Dream + MemoryProvider

**目标**：三级流转完整闭环

- [ ] 新建 `memory/flush.py`：对话摘要 → 日记忆
- [ ] 新建 `memory/dream.py`：日记忆 → MEMORY.md 蒸馏
- [ ] 修改 `agent/prompt/providers.py`：MemoryProvider 实现
- [ ] AgentLoop 集成：flush 触发 + MemoryProvider 注入
- [ ] CLI 命令：`/dream` 手动触发蒸馏
- [ ] 配置扩展：`config.yaml` 增加 memory 段

**验收**：
- 对话超过阈值 → 自动生成日记忆
- `/dream` → 日记忆蒸馏到 MEMORY.md
- system prompt 中出现记忆 section

### Step 5：集成测试 + 打磨

- [ ] 端到端测试：多轮对话 → flush → dream → search → 注入
- [ ] 性能测试：1000+ chunks 下的检索延迟
- [ ] 错误恢复：FTS5 损坏自愈、embedding 失败降级
- [ ] 文档：README 更新 + memory 系统使用指南

---

## 8. 简历可写内容

完成 Phase 4 后，简历可新增：

### 记忆系统（Phase 4）
- **三级记忆架构**：设计并实现短期（会话内）→ 中期（日记忆摘要）→ 长期（蒸馏知识库）的记忆流转机制
- **混合检索引擎**：SQLite FTS5（unicode61 + trigram 双索引）+ numpy 向量余弦相似度，支持中英文混合查询，三级降级策略（FTS5 → trigram → LIKE）
- **增量同步算法**：两阶段文件变更检测（hash 比对 → 批量 embedding），embedding 失败时保留原索引、下次重试
- **Deep Dream 蒸馏**：LLM 驱动的日记忆→长期记忆蒸馏，支持定时/手动/自动三种触发模式
- **时间衰减排序**：日记忆文件按半衰期指数衰减，长期记忆常青不衰减
- **CJK 搜索优化**：trigram FTS5 三字符滑窗索引 + Unicode 范围检测，解决中文分词缺失问题
- **自愈机制**：FTS5 shadow table 损坏自动检测 + 从源数据重建，UPSERT 保证 rowid 稳定性

---

## 9. 与 CowAgent 的对比总结

| 维度 | CowAgent | dotClaw (Phase 4 设计) |
|------|----------|----------------------|
| 用户模型 | 多用户 + scope (shared/user/session) | 单用户，简化 scope |
| Embedding 供应商 | 5 个 (openai/linkai/dashscope/doubao/zhipu) | 初期 1 个 (openai-compatible) |
| 向量存储 | SQLite BLOB | 同，复用 |
| FTS5 | 双索引 (unicode61 + trigram) | 同，复用 |
| Dream 日记 | 独立 `dreams/` 目录 | 简化，直接蒸馏到 MEMORY.md |
| 记忆工具 | memory_get + memory_search 独立工具 | 通过 MemoryProvider 注入 prompt |
| MemoryService | 云协议接口 | 不需要（纯本地 CLI） |
| FlushManager | 异步摘要 + 去重 hash | 同，复用 |
| 时间衰减 | 30 天半衰期指数衰减 | 同，可配置 |
| 依赖 | numpy 可选 | numpy 推荐，缺失降级 |

**设计哲学差异**：
- CowAgent：面向多用户云部署，功能完备但复杂度高
- dotClaw：面向个人 CLI 工具，核心原理相同但简化用户体系，专注学习价值

---

## 10. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| Embedding API 延迟 | 搜索和同步慢 | EmbeddingCache + batch + 异步 |
| SQLite 并发写入 | 数据库锁 | WAL 模式 + RLock |
| FTS5 不可用（编译选项） | 中文搜索退化为 LIKE | 运行时检测 + 降级 |
| numpy 未安装 | 向量检索性能差 100x | 纯 Python fallback + 安装提示 |
| 记忆注入占用 token 预算 | 压缩可用上下文 | max_context_tokens 裁剪 + 摘要压缩 |
| LLM 摘要质量差 | 日记忆/蒸馏不准确 | 调优 prompt + 用户可编辑 |



## v1 → v2 主要变更

### 🔴 架构层面（最大变化）

| 维度       | v1（旧）                                   | v2（新，融入设计文档）                                       |
| ---------- | ------------------------------------------ | ------------------------------------------------------------ |
| 新增文件数 | 1 个（`memory/flush.py`）                  | 7 个（storage/chunker/embedding/manager/flush/dream/config） |
| 存储引擎   | 纯 Markdown 文件                           | SQLite + FTS5 双索引 + embedding BLOB                        |
| 检索方式   | `get_context()` 硬截最后 N 字符            | `MemoryManager.search()` 混合检索（向量0.7 + FTS5 0.3 + 时间衰减） |
| 去重       | SHA256 行级（仅防完全重复）                | SHA256 行级 + LLM 语义合并（蒸馏 prompt 中要求）             |
| 蒸馏标记   | 文件末尾 `--- distilled` 行                | 独立 `.dream_state.json` 元数据文件                          |
| flush 输入 | `AgentResult.final_text[:500]`（无上下文） | 最近 N 条完整 `SessionMessage`（含 user↔assistant 往返）     |

### 🟡 之前我提的 7 个问题全部解决

| 问题               | v1   | v2 解决方式                                                  |
| ------------------ | ---- | ------------------------------------------------------------ |
| summarize 缺上下文 | ❌    | ✅ 传完整 messages 列表                                       |
| get_context 硬截断 | ❌    | ✅ MemoryManager.search() 语义检索                            |
| 去重粒度不够       | ❌    | ✅ LLM 蒸馏 prompt 合并语义相近条目                           |
| distilled 标记脆弱 | ❌    | ✅ 独立 .dream_state.json                                     |
| session_id 无意义  | ❌    | ✅ 不再传入（用 messages 替代）                               |
| 私有属性访问       | ❌    | ✅ flush 从 self.session.messages 获取                        |
| 职责过重           | ❌    | ✅ 拆为 7 个类：Storage/Chunker/Embedding/Manager/Flush/Dream/Config |

### 🟢 从设计文档新加入的内容

- **双 FTS5**：unicode61（英文）+ trigram（中文 CJK），三级降级（FTS5 → trigram → LIKE）
- **EmbeddingProvider**：OpenAI-compatible 抽象，复用 LLM 层的 API 模式，None 时降级为纯关键词
- **TextChunker**：按行 + token 估算分块，Markdown 结构感知
- **EmbeddingCache**：会话级 LRU 缓存，避免重复 API 调用
- **MemoryConfig**：完整配置体系（16 个字段）
- **UPSERT 约束**：避免 FTS5 rowid 漂移（CowAgent 踩过的坑）
- **自愈机制**：FTS5 shadow table 损坏自动重建
- **时间衰减**：日记忆按 30 天半衰期衰减，MEMORY.md 常青

### 🔵 我保留/调整的设计决策

- 不引入 ChromaDB/FAISS/LangChain/Mem0（与设计文档一致）
- flush 触发从"每次 run() 后"改为"消息数超过阈值"（更有节制）
- MemoryProvider 同时保留 `provide()` 同步降级 + `provide_async()` 语义检索双通道
- 依赖：新增 numpy + tiktoken（numpy 可选降级）
