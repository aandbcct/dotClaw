# dotClaw 记忆系统架构文档

> **版本**: v1.1 | **对应 Phase**: P4 | **更新日期**: 2026-06-01
> **维护说明**: 本文档随记忆系统演进持续更新，每次架构变更请同步更新版本号和变更说明。

---

## 1. 架构总览

dotClaw 记忆系统采用**三级记忆架构**，从短期到长期逐级蒸馏，兼顾检索效率与持久化质量。

```
┌─────────────────────────────────────────────────────────────────────┐
│                       dotClaw Agent 进程                          │
│                                                                 │
│  ┌──────────────┐    ┌──────────────────────────────────────┐    │
│  │  AgentLoop   │───►│       PromptBuilder                 │    │
│  │  (主循环)     │    │  [Role] [Rules] [Tools] [Memory]  │    │
│  └──────┬───────┘    └──────────────────┬───────────────┘    │
│          │                                │                       │
│          │  user_message                  │ system_prompt         │
│          ▼                                ▼                       │
│  ┌──────────────────────┐    ┌─────────────────────────┐      │
│  │  _build_context()     │    │  MemoryProvider          │      │
│  │  await search()       │◄───│  .provide(context)      │      │
│  └──────────────────────┘    └─────────────────────────┘      │
│          │                                                       │
│          ▼                                                       │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │                   MemoryManager                          │    │
│  │  核心调度：search() / sync() / flush_memory()          │    │
│  └──┬──────────┬──────────────┬──────────────┬─────────┘    │
│     │          │              │              │                     │
│     ▼          ▼              ▼              ▼                     │
│  ┌──────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐         │
│  │Storage│ │ Chunker   │ │Embedding │ │ FlushMgr │         │
│  │(SQLite)│ │(分块)    │ │Provider  │ │(L2写入) │         │
│  └──────┘ └──────────┘ └──────────┘ └────┬─────┘         │
│        │                                      │                 │
│        ▼                                      ▼                 │
│  ┌──────────────┐                    ┌──────────────┐          │
│  │ DeepDream     │                    │ YYYY-MM-DD.md │          │
│  │ (L3 蒸馏)    │                    │ (日记忆文件)  │          │
│  └──────┬───────┘                    └──────────────┘          │
│         │                                                             │
│         ▼                                                             │
│  ┌──────────────┐                                                    │
│  │  MEMORY.md   │  (长期记忆文件)                                    │
│  └──────────────┘                                                    │
└─────────────────────────────────────────────────────────────────────┘

        外部存储（文件系统 + SQLite）
```

---

## 2. 三级记忆数据流

```
用户消息
   │
   ▼
┌──────────────────────────────────────────────────────┐
│ L1  工作记忆 (SessionManager)                      │
│ 存储: data/sessions/<id>.json                    │
│ 内容: 完整对话历史（user + assistant 消息列表）     │
│ 生命周期: 单次会话，切换会话即切换上下文            │
│ 触发: 每次 AgentLoop.run() 自动读写               │
└──────────────────────┬───────────────────────────┘
                         │ 消息数 > flush_threshold
                         ▼
┌──────────────────────────────────────────────────────┐
│ L2  日记忆 (MemoryFlushManager)                    │
│ 存储: data/memory/YYYY-MM-DD.md                  │
│ 内容: LLM 生成的对话摘要（2-3 句中文）            │
│ 生命周期: 天级，蒸馏到 L3 后可归档                │
│ 触发: AgentLoop.run() 末尾异步 asyncio.create_task │
│ 去重: 同日 content hash 比对，相同则跳过           │
└──────────────────────┬───────────────────────────┘
                         │ /dream 命令 or 定时触发
                         ▼
┌──────────────────────────────────────────────────────┐
│ L3  长期记忆 (DeepDream + MEMORY.md)              │
│ 存储: data/memory/MEMORY.md                      │
│ 内容: 蒸馏后的核心知识（用户偏好/决策/待办/知识） │
│ 生命周期: 永久，持续增长，LLM 语义合并去重         │
│ 状态: data/memory/.dream_state.json（蒸馏元数据）  │
└──────────────────────────────────────────────────────┘
```

---

## 3. 模块详解

### 3.1 `SessionManager` — L1 工作记忆

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/memory/store.py` |
| **存储** | `data/sessions/<session_id>.json` |
| **职责** | 会话 CRUD、消息列表持久化 |
| **读写时机** | `AgentLoop.run()` 末尾 `save()` |

**数据格式**（JSON）：
```json
{
  "id": "a1b2c3d4",
  "title": "API 设计讨论",
  "created_at": "2026-06-01T10:00:00",
  "updated_at": "2026-06-01T10:30:00",
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```

**与其他模块关系**：
- `AgentLoop` 持有 `SessionManager` 引用，每次 `run()` 后 `save()`
- L2 flush 直接从 `session.messages` 读取最近 N 条消息，不依赖 `SessionManager` 额外接口

---

### 3.2 `MemoryFlushManager` — L2 写入

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/memory/flush.py` |
| **存储** | `data/memory/YYYY-MM-DD.md` |
| **职责** | 对话摘要生成 + 日记忆写入 + 同日去重 |
| **触发** | `AgentLoop.run()` 成功路径末尾（异步） |

**调用链**：
```
AgentLoop.run()
  └─ asyncio.create_task(
       memory_mgr.flush_memory(messages, reason="threshold")
     )
       └─ MemoryFlushManager.flush_from_messages(messages, reason, max_messages)
            ├─ _summarize_with_llm(recent_messages)  → LLM 生成摘要
            ├─ 同日 content_hash 去重（避免重复写入）
            └─ 追加写入 data/memory/YYYY-MM-DD.md
```

**写入格式**（Markdown）：
```markdown
## 14:30
- 用户讨论了：API 路由设计，提出用 purpose 做意图分类
- AI 回复：建议参考 LiteLLM 路由策略，给出 3 种方案对比

## 15:05
- 用户询问：SQLite FTS5 中文分词方案
- AI 回复：推荐 trigram tokenizer，附代码示例
```

**异常行为**：
- LLM 调用失败 → 降级为模板摘要（`_generate_fallback_summary`）
- 异常静默，不影响主流程（`asyncio.create_task` 后台执行）

---

### 3.3 `DeepDream` — L3 蒸馏

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/memory/dream.py` |
| **存储** | `data/memory/MEMORY.md` + `.dream_state.json` |
| **职责** | 日记忆 → 长期记忆的 LLM 语义蒸馏 |
| **触发** | `/dream` CLI 命令（手动）；cron 定时（P8） |

**调用链**：
```
/dream 命令
  └─ DeepDream.run(force=False)
       ├─ _load_state()  →  .dream_state.json
       ├─ 扫描 data/memory/YYYY-MM-DD.md
       ├─ 过滤已蒸馏日期（force=True 时跳过过滤）
       ├─ _distill_with_llm(existing_memory, new_contents, dates)
       │    └─ LLM 调用：合并 + 语义去重 + 提炼
       ├─ 写入 MEMORY.md（覆盖模式）
       ├─ 更新 .dream_state.json
       └─ 返回 "已蒸馏 N 日记忆"
```

**.dream_state.json 格式**：
```json
{
  "2026-05-30": {
    "distilled_at": "2026-05-31T23:55:00",
    "entries": 12,
    "hash": "a3f2b1c4d5e6f7a"
  },
  "2026-05-31": {
    "distilled_at": null
  }
}
```

**蒸馏 Prompt 设计**（system）：
```
你是记忆提炼助手。阅读以下对话摘要和已有长期记忆，合并提炼为简洁的 Markdown 列表。
要求：
1. 提取用户偏好、重要决策、待办事项、学到的知识
2. 与已有记忆语义相近的条目合并而非新增
3. 忽略闲聊和重复信息
4. 每行格式：'- [日期] 内容'
```

---

### 3.4 `MemoryStorage` — 存储层（SQLite + FTS5 + 向量）

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/memory/storage.py` |
| **存储** | `data/memory/memory.db`（SQLite WAL 模式） |
| **职责** | 分块存储、双 FTS5 索引、向量检索、文件变更检测 |

**SQLite Schema**：

```sql
-- 核心分块表
CREATE TABLE chunks (
    id          TEXT PRIMARY KEY,        -- SHA256(path + start_line)
    path        TEXT NOT NULL,           -- 来源文件相对路径
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    text        TEXT NOT NULL,           -- 分块文本
    embedding   BLOB,                   -- np.float32.tobytes()
    hash        TEXT NOT NULL,           -- 内容 hash（去重）
    source      TEXT NOT NULL DEFAULT 'memory',  -- memory/session/knowledge
    metadata    TEXT,                   -- JSON 扩展字段
    created_at  INTEGER,
    updated_at  INTEGER
);

-- 文件元数据（变更检测）
CREATE TABLE files (
    path   TEXT PRIMARY KEY,
    hash   TEXT NOT NULL,    -- SHA256(file_content)
    mtime  INTEGER NOT NULL, -- os.path.getmtime
    size   INTEGER NOT NULL,  -- len(file_bytes)
    updated_at INTEGER
);

-- FTS5 英文索引（unicode61 tokenizer）
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text, id UNINDEXED, path UNINDEXED, source UNINDEXED,
    content='chunks', content_rowid='rowid'
);

-- FTS5 中文索引（trigram tokenizer，3字符滑窗）
CREATE VIRTUAL TABLE chunks_fts_trigram USING fts5(
    text, id UNINDEXED, path UNINDEXED, source UNINDEXED,
    content='chunks', content_rowid='rowid',
    tokenize='trigram case_sensitive 0'
);
```

**关键设计决策**：
- `UPSERT`（而非 `INSERT OR REPLACE`）→ 保护 FTS5 `content_rowid` 不漂移
- `embedding` 用 `np.float32.tobytes()` BLOB 存储 → 比 JSON 小 6 倍
- 双 FTS5 → 英文走 `unicode61`，中文/CJK 走 `trigram`
- 自愈 → FTS5 shadow table 损坏时从 `chunks` 表重建

**对外方法**：

| 方法 | 输入 | 输出 | 说明 |
|------|------|------|------|
| `search_keyword(query, limit)` | 查询字符串 | `list[SearchResult]` | FTS5 → trigram → LIKE 三级降级 |
| `search_vector(embedding, limit)` | `list[float]` | `list[SearchResult]` | numpy 余弦相似度，全表扫描 |
| `save_chunks_batch(chunks)` | `list[MemoryChunk]` | — | UPSERT + 触发 FTS5 重建 |
| `delete_by_path(path)` | 文件路径 | — | 删除该文件所有 chunk |
| `get_file_state(path)` | 文件路径 | `(hash, mtime, size) or None` | 文件变更检测 |
| `upsert_file_state(path, hash, mtime, size)` | — | — | 更新文件元数据 |

---

### 3.5 `TextChunker` — 文本分块

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/memory/chunker.py` |
| **职责** | Markdown 文本 → 语义完整分块 |

**分块策略**：
1. 按行遍历，累计 token 数（estimate）
2. 遇到 `##` 标题且当前块非空 → 优先切分（保护 Markdown 结构）
3. 达到 `max_tokens` → 强制切分
4. 块间保留 `overlap_tokens` 行重叠（避免语义截断）

**输出**：`list[TextChunk]`（每个 chunk 含 `text / start_line / end_line`）

**Token 估算**（中英文差异化公式）：
```python
def _estimate_tokens(text: str) -> int:
    chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    others = len(text) - chinese
    return chinese + (others // 4)
```
> 使用中英文差异化估算（中文 ~1 char/token，英文 ~4 chars/token），`trim()` 预留 20% 安全边界。

---

### 3.6 `EmbeddingProvider` — 向量嵌入

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/memory/embedding.py` |
| **职责** | 文本 → 向量（OpenAI-compatible API） |

**类层次**：
```
EmbeddingProvider (ABC)
  └─ OpenAIEmbeddingProvider
       ├─ embed_query(text)  → list[float]
       └─ embed_batch(texts) → list[list[float]]  （内部按 batch_size=16 分页）
```

**配置来源**（按优先级）：
1. `config.memory.embedding_api_base`（若非空）
2. 回退到 `config.llm` 的 `api_base`（共享 LLM 端点）
3. `config.memory.embedding_api_key`（若非空）→ 否则共享 `config.llm.api_key`

**降级策略**：
- `embedding_provider=None`（未配置）→ `MemoryManager.search()` 跳过向量检索，仅走关键词
- numpy 未安装 → `search_vector()` 用纯 Python 余弦相似度（性能差 ~100x，打印 warning）

**EmbeddingCache**（LRU，max 256 条）：
- 键：`hashlib.sha256(text).hexdigest()[:16]`
- 避免相同查询重复调用 embedding API

---

### 3.7 `MemoryManager` — 核心调度器

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/memory/manager.py` |
| **职责** | 统一检索入口、sync 调度、flush 委托 |

**核心方法**：

#### `search(query, max_results, min_score) -> list[SearchResult]`

```
search(query)
  ├─ [if sync_on_search] await sync()           → 检查文件变更
  ├─ [if embedding_provider] embedding = _get_embedding(query)
  │    └─ _get_embedding(text)
  │         ├─ [if cache hit] → 返回缓存
  │         └─ embedding_provider.embed_query(text)
  │              └─ [if cache] 写入 EmbeddingCache
  ├─ [if embedding] storage.search_vector(embedding, limit*2)  → vector_results
  ├─ storage.search_keyword(query, limit*2)                    → keyword_results
  ├─ _apply_temporal_decay(vector_results)       → 时间衰减
  ├─ _apply_temporal_decay(keyword_results)
  ├─ 加权合并（vector_weight=0.7, keyword_weight=0.3）
  ├─ 按 score 降序排序
  └─ 过滤 min_score + 截取 max_results
```

**加权合并算法**：
```python
merged = {}
for r in vector_results:
    key = f"{r.path}:{r.start_line}"
    r.score *= vector_weight          # 默认 0.7
    merged[key] = r

for r in keyword_results:
    key = f"{r.path}:{r.start_line}"
    if key in merged:
        merged[key].score += r.score * keyword_weight
    else:
        r.score *= keyword_weight    # 默认 0.3
        merged[key] = r

return sorted(merged.values(), key=lambda r: r.score, reverse=True)[:max_results]
```

#### `sync(force=False) -> None`

```
sync()
  ├─ 遍历监控文件列表：[MEMORY.md, skills/**/*.md]
  ├─ storage.get_file_state(path) → 比对 hash/mtime/size
  ├─ [if dirty or force]
  │    ├─ storage.delete_by_path(path)
  │    ├─ chunker.chunk_text(file_content)  →  list[TextChunk]
  │    ├─ [if embedding_provider] embed_batch(all_chunks_text) → embeddings
  │    ├─ 组装 list[MemoryChunk]（含 embedding BLOB）
  │    ├─ storage.save_chunks_batch(chunks)
  │    └─ storage.upsert_file_state(path, hash, mtime, size)
  └─ [if clean] 跳过
```

#### `flush_memory(messages, reason) -> bool`

```
flush_memory(messages, reason)
  └─ [if flush_mgr] flush_mgr.flush_from_messages(messages, reason)
```

---

### 3.8 `MemoryProvider` — Prompt 注入

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/agent/prompt/providers.py` |
| **职责** | 将检索结果注入 system prompt 的 `## 相关记忆` section |

**数据流（单一通路）**：

```
AgentLoop._build_context(user_message)     [async]
  │
  ├─ memory_mgr.search(user_message)  →  list[SearchResult]
  │
  ├─ 格式化为 memory_summary 字符串：
  │    "- (source:path) snippet text"
  │
  └─ AgentContext(memory_summary=memory_summary)
       │
       ▼
  MemoryProvider.provide(context)          [同步]
       │
       ├─ [if not context.memory_summary] → return None（跳过该 section）
       └─ return f"## 相关记忆\n\n{context.memory_summary}"
            │
            ▼
       PromptBuilder.build(context)
            │
            ├─ RoleProvider.provide()    →  ## 角色定义
            ├─ RulesProvider.provide()    →  ## 行为规则（可选）
            ├─ ToolsProvider.provide()    →  ## 可用工具
            └─ MemoryProvider.provide()   →  ## 相关记忆（可选）
                 │
                 ▼
            system_prompt（完整，含记忆上下文）
```

> **设计要点**：`MemoryProvider.provide()` 是纯同步方法。异步检索在 `_build_context()` 中完成，结果存入 `AgentContext.memory_summary`，`provide()` 只做格式化读取。这符合 `DataProvider` 接口的同步约束。

---

## 4. 完整调用链路时序图

### 4.1 正常对话 + 记忆检索 + Flush

```
用户                  AgentLoop           MemoryManager        MemoryStorage       LLM
 │                       │                     │                    │               │
 │── "API 怎么设计？" ──►                     │                    │               │
 │                       ├─ _build_context()   │                    │               │
 │                       │                     │                    │               │
 │                       ├─ await search() ───►│                    │               │
 │                       │   ├─ sync()         │── 检查文件变更 ───►│               │
 │                       │   ├─ embed_query()  │                    │               │
 │                       │   ├─ search_vector()│── 向量检索 ───────►│               │
 │                       │   ├─ search_keyword()│── FTS5 检索 ──────►│               │
 │                       │   └─ 加权合并       │                    │               │
 │                       │                     │                    │               │
 │                       ├─ build system       │                    │               │
 │                       │   prompt (含记忆)    │                    │               │
 │                       │                     │                    │               │
 │                       ├─ LLM.chat() ───────┼──────────────────────────────────►
 │                       │                     │                    │     │
 │◄── "API 设计建议..." ──┤                     │                    │               │
 │                       │                     │                    │               │
 │                       ├─ session.save()     │                    │               │
 │                       │                     │                    │               │
 │                       └─ flush_memory() ──►│                    │               │
 │                         (asyncio.create_task)                   │               │
 │                                             ├─ LLM 生成摘要 ───────────────────►
 │                                             └─ 写入 YYYY-MM-DD.md            │
```

### 4.2 `/dream` 蒸馏链路

```
用户            AgentLoop       main.py (_cmd_dream)     DeepDream        LLM
 │                 │                │                     │               │
 │── "/dream" ───►│                │                     │               │
 │                 ├─ 命令路由     │                     │               │
 │                 │                ├─ DeepDream.run() ──►                │
 │                 │                │                     │               │
 │                 │                │                     ├─ 读日记忆文件 │               │
 │                 │                │                     ├─ 读 MEMORY.md │               │
 │                 │                │                     │               │
 │                 │                │                     ├─ distill_with_llm()          │
 │                 │                │                     └─ LLM 调用 ───────────────────►
 │                 │                │                     │               │
 │                 │                │                     ├─ 写 MEMORY.md               │
 │                 │                │                     ├─ 更新 .dream_state.json      │
 │                 │                │                     │               │
 │◄── "已蒸馏 3 日" ─────────────┤                     │               │
```

---

## 5. 配置参考

`config.yaml` 中的 `memory:` 段：

```yaml
memory:
  # === 基础路径 ===
  long_term_file: "./data/memory/MEMORY.md"   # L3 长期记忆文件
  workspace: "./data"                           # 工作空间根目录
  db_path: "./data/memory/memory.db"            # SQLite 数据库路径

  # === 分块 ===
  chunk_max_tokens: 500                          # 每块最大 token 数
  chunk_overlap_tokens: 50                       # 块间重叠 token 数

  # === Embedding ===
  embedding_provider: "openai"                   # null = 纯关键词模式
  embedding_model: "text-embedding-v3"           # 嵌入模型名
  embedding_dimensions: 1024                      # 向量维度
  embedding_api_base: ""                          # 留空 = 跟随 llm.api_base
  embedding_api_key: ""                           # 留空 = 跟随 llm.api_key

  # === 检索 ===
  max_results: 5                                 # 返回最多 N 条结果
  min_score: 0.1                                 # 最低相关分数（过滤噪声）
  vector_weight: 0.7                              # 向量检索权重
  keyword_weight: 0.3                             # 关键词检索权重
  sync_on_search: true                            # 搜索前自动检查文件变更
  temporal_decay_half_life_days: 30.0             # 日记忆时间衰减半衰期

  # === L2 Flush ===
  flush_threshold: 20                             # 消息数超此值触发 flush
  flush_max_messages: 10                           # flush 时取最近 N 条消息

  # === L3 Dream ===
  dream_enabled: true                             # 是否启用蒸馏
  dream_schedule: "55 23 * * *"                   # cron 表达式（P8 启用）
```

---

## 6. 初始化链路（`main.py`）

```
main.py / _run_cli()
  │
  ├─ 1. load_config() → Config 对象
  │
  ├─ 2. SessionManager(config.session.directory)
  │
  ├─ 3. LLMProxy (ModelRouter + RateLimiter)
  │
  ├─ 4. Memory 系统初始化（if config.memory）
  │    │
  │    ├─ MemoryStorage(get_db_path(project_root))
  │    │    └─ SQLite 连接 + 建表 + FTS5 索引
  │    │
  │    ├─ TextChunker(chunk_max_tokens, chunk_overlap_tokens)
  │    │
  │    ├─ [if embedding_provider + api_key]
  │    │    └─ OpenAIEmbeddingProvider(api_base, api_key, model, dimensions)
  │    │
  │    ├─ EmbeddingCache(max_size=256)
  │    │
  │    ├─ MemoryFlushManager(workspace_dir, llm=llm_proxy)
  │    │
  │    ├─ MemoryManager(storage, chunker, embedding, flush_mgr, cache, ...)
  │    │
  │    └─ DeepDream(workspace_dir, llm=llm_proxy)
  │
  ├─ 5. PromptBuilder([RoleProvider, RulesProvider, ToolsProvider, MemoryProvider])
  │         │
  │         └─ MemoryProvider(memory_mgr)   ← P4 激活点
  │
  ├─ 6. AgentLoop(llm, session, session_mgr, channel, config,
  │                tool_registry, prompt_builder, logger, memory_mgr)
  │
  └─ 7. 主循环：await channel.receive() → await agent.run(user_input)
```

---

*本文档由 dotClaw 开发工程师维护。架构变更后请同步更新此文档。*
开发日志见 `docs/phase4-record.md`。
