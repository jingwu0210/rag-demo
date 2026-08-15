# RAG QA Service — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 从零构建完整 RAG QA 服务：文档摄入 → 多轮问答 → 一键评估。

**Architecture:** 模块化自建（无 RAG 框架依赖）。FastAPI + asyncio，Core Engine 按 Query/Ingest 两条独立链路，策略模式切换检索配置，Adapter 模式替换 LLM Provider。

**Tech Stack:** Python 3.11+, FastAPI, ChromaDB, sentence-transformers, rank-bm25, PaddleOCR, PyMuPDF, aiosqlite, httpx, structlog, Ragas

## Global Constraints

- 单实例进程内运行（无 Redis/Milvus/Docker）
- 90% 请求 ≤ 10s，≥ 5 并发；Semaphore(10) 安全上限
- 所有行为 config.yaml 驱动，改配置不改代码
- RRF k=60, AdaptiveK [3,8]/min_score=0.45
- BGE-M3 本地 Embedding (MPS)，DeepSeek Flash API 默认生成
- 答案严格基于检索上下文，二层注入防御
- structlog JSON 日志 + TraceID 透传
- 版本化入库：doc_hash + is_active + 事务替换

---

## Phase 1: 项目骨架 & 配置基础

### Task 1.1: 项目目录 & 依赖

**Files:**
- Create: `requirements.txt`
- Create: `config.yaml`
- Create: `data/.gitkeep`, `data/corpus/.gitkeep`, `data/logs/.gitkeep`

**Interfaces:**
- Produces: 项目根目录结构，所有模块导入路径确定

- [ ] **Step 1: 创建目录结构**

```bash
mkdir -p api core services storage eval
mkdir -p data/{corpus,chroma,ocr,logs,eval/results}
touch data/{corpus,chroma,ocr,logs}/.gitkeep
```

- [ ] **Step 2: 写入 requirements.txt**

```
fastapi==0.115.*
uvicorn[standard]==0.34.*
httpx==0.28.*
pydantic==2.*

chromadb==0.5.*
sentence-transformers==3.*
rank-bm25==0.2.*
jieba==0.42.*

pymupdf==1.24.*
paddlepaddle==3.*
paddleocr==2.*
python-docx==1.*

aiosqlite==0.20.*
structlog==24.*

ragas==0.2.*
pandas==2.*
pyyaml==6.*
```

- [ ] **Step 3: 验证环境**

```bash
pip install -r requirements.txt
python -c "import chromadb, fastapi, aiosqlite, structlog, fitz; print('OK')"
```

- [ ] **Step 4: 写入 .gitignore**

```
data/corpus/*
data/chroma/*
data/ocr/*
data/logs/*
data/eval/results/*
data/cache.db
__pycache__/
*.pyc
.env
```

---

### Task 1.2: ConfigRegistry 单例

**Files:**
- Create: `core/__init__.py` (empty)
- Create: `core/config.py`

**Interfaces:**
- Produces: `ConfigRegistry.init(path)`, `ConfigRegistry.get(key_path, default=None)`, `ConfigRegistry.override(key_path, value)`

- [ ] **Step 1: 写入 core/config.py**

```python
import os
import yaml
from typing import Any, Optional

class ConfigRegistry:
    _instance: Optional["ConfigRegistry"] = None

    def __init__(self, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            self._data = yaml.safe_load(f)
        self._apply_env_overrides()

    @classmethod
    def init(cls, config_path: str = "config.yaml") -> "ConfigRegistry":
        cls._instance = cls(config_path)
        return cls._instance

    @classmethod
    def get(cls, key_path: str, default: Any = None) -> Any:
        keys = key_path.split(".")
        value = cls._instance._data
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    @classmethod
    def override(cls, key_path: str, value: Any) -> None:
        keys = key_path.split(".")
        target = cls._instance._data
        for k in keys[:-1]:
            if k not in target:
                target[k] = {}
            target = target[k]
        target[keys[-1]] = value

    def _apply_env_overrides(self) -> None:
        for key, val in os.environ.items():
            if key.startswith("RAG_"):
                config_key = key[4:].lower().replace("__", ".")
                self._set_nested(self._data, config_key, val)

    @staticmethod
    def _set_nested(data: dict, key_path: str, value: str) -> None:
        keys = key_path.split(".")
        for k in keys[:-1]:
            if k not in data:
                data[k] = {}
            data = data[k]
        data[keys[-1]] = yaml.safe_load(value) if value.isdigit() else value
```

- [ ] **Step 2: 验证 ConfigRegistry**

```python
# 临时测试脚本
from core.config import ConfigRegistry
ConfigRegistry.init("config.yaml")
assert ConfigRegistry.get("retrieval.mode") == "hybrid+rerank"
ConfigRegistry.override("retrieval.mode", "vector-only")
assert ConfigRegistry.get("retrieval.mode") == "vector-only"
print("OK")
```

---

### Task 1.3: config.yaml 完整配置

**Files:**
- Create: `config.yaml`

- [ ] **Step 1: 写入 config.yaml**（从设计文档 §10.1 / §3.4 复制完整配置，包含 LLM / Embedding / Reranker / 检索 / 缓存 / 分片 / OCR / PII / 拒答 / 注入扫描 / 并发 / 多轮 / 对话压缩 / ChromaDB / 文档类型 / 过期过滤 / 日志 / 评估 / 路径 全部配置域）

---

## Phase 2: Storage Layer

### Task 2.1: ChromaDB 客户端封装

**Files:**
- Create: `storage/__init__.py` (empty)
- Create: `storage/chroma_client.py`

**Interfaces:**
- Consumes: `ConfigRegistry.get("chromadb.*")`
- Produces: `ChromaStore.add(ids, documents, embeddings, metadatas)`, `ChromaStore.query(vec, top_k, where)`, `ChromaStore.update_metadata(where, metadata)`, `ChromaStore.collection`

- [ ] **Step 1: 写入 storage/chroma_client.py**

```python
import chromadb
from chromadb.config import Settings
from core.config import ConfigRegistry

class ChromaStore:
    def __init__(self):
        persist_dir = ConfigRegistry.get("chromadb.persist_directory", "data/chroma")
        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False)
        )
        self._collection = self._client.get_or_create_collection(
            name=ConfigRegistry.get("chromadb.collection_name", "knowledge_base"),
            metadata={"hnsw:space": ConfigRegistry.get("chromadb.distance_metric", "cosine")}
        )

    @property
    def collection(self):
        return self._collection

    def add(self, ids, documents, embeddings, metadatas):
        self._collection.add(ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas)

    def query(self, query_embeddings, top_k=20, where=None):
        kwargs = {"query_embeddings": [query_embeddings], "n_results": top_k}
        if where:
            kwargs["where"] = where
        results = self._collection.query(**kwargs)
        return self._format_results(results)

    def update_metadata(self, where, metadata):
        """批量更新 metadata（如 is_active=False 软删除）"""
        results = self._collection.get(where=where)
        if results["ids"]:
            for i in range(len(results["ids"])):
                updated = {**results["metadatas"][i], **metadata}
                self._collection.update(ids=[results["ids"][i]], metadatas=[updated])
            return len(results["ids"])
        return 0

    def _format_results(self, raw):
        docs = []
        if not raw["ids"] or not raw["ids"][0]:
            return docs
        for i, chunk_id in enumerate(raw["ids"][0]):
            docs.append({
                "chunk_id": chunk_id,
                "text": raw["documents"][0][i],
                "score": 1.0 - (raw["distances"][0][i] if raw.get("distances") else 0),
                "metadata": raw["metadatas"][0][i] if raw.get("metadatas") else {}
            })
        return docs
```

---

### Task 2.2: SQLite 客户端 & 表初始化

**Files:**
- Create: `storage/sqlite_client.py`

**Interfaces:**
- Consumes: `ConfigRegistry.get("paths.sqlite")`
- Produces: `Database.get_db()` → aiosqlite connection; 自动建表 (cache_entries, request_metrics, sessions, turns, eval_history)

- [ ] **Step 1: 写入 storage/sqlite_client.py**

```python
import aiosqlite
from core.config import ConfigRegistry

DB_PATH = ConfigRegistry.get("paths.sqlite", "data/cache.db")

async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    return db

async def init_db():
    db = await get_db()
    # cache_entries
    await db.execute("""
        CREATE TABLE IF NOT EXISTS cache_entries (
            cache_key TEXT PRIMARY KEY, query TEXT NOT NULL, answer TEXT NOT NULL,
            sources_json TEXT, token_usage INTEGER, retrieval_mode TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_cache_created ON cache_entries(created_at)")

    # request_metrics
    await db.execute("""
        CREATE TABLE IF NOT EXISTS request_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
            session_id TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            latency_retrieval INTEGER, latency_rerank INTEGER, latency_generation INTEGER, latency_total INTEGER,
            token_prompt INTEGER, token_completion INTEGER, token_total INTEGER,
            retrieval_mode TEXT, cache_hit BOOLEAN DEFAULT FALSE,
            refused BOOLEAN DEFAULT FALSE, refusal_reason TEXT,
            timeout BOOLEAN DEFAULT FALSE, degraded BOOLEAN DEFAULT FALSE, error TEXT,
            pii_redact_count INTEGER DEFAULT 0, injection_blocked INTEGER DEFAULT 0,
            faithfulness_score REAL, context_precision REAL, answer_compliance REAL
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_metrics_ts ON request_metrics(timestamp)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_metrics_mode ON request_metrics(retrieval_mode)")

    # sessions
    await db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'active', turn_count INTEGER DEFAULT 0
        )
    """)

    # turns
    await db.execute("""
        CREATE TABLE IF NOT EXISTS turns (
            turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
            turn_index INTEGER NOT NULL, raw_query TEXT NOT NULL, resolved_query TEXT,
            query_language TEXT, answer TEXT, refused BOOLEAN DEFAULT FALSE,
            refusal_reason TEXT, from_cache BOOLEAN DEFAULT FALSE,
            retrieval_mode TEXT, sources_json TEXT,
            timing_json TEXT, token_prompt INTEGER, token_completion INTEGER, token_total INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, turn_index)")

    # eval_history
    await db.execute("""
        CREATE TABLE IF NOT EXISTS eval_history (
            run_id TEXT PRIMARY KEY, config_name TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, test_set_hash TEXT NOT NULL,
            total_qa_pairs INTEGER,
            faithfulness REAL, context_precision REAL, context_recall REAL, answer_relevancy REAL,
            answer_compliance REAL, style_consistency REAL, refusal_appropriateness REAL,
            p50_latency_ms INTEGER, p95_latency_ms INTEGER, avg_tokens_per_call INTEGER,
            total_pii_redactions INTEGER DEFAULT 0, total_injections_blocked INTEGER DEFAULT 0,
            per_qa_results_json TEXT
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_eval_history_ts ON eval_history(timestamp)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_eval_history_config ON eval_history(config_name)")

    await db.commit()
    await db.close()
```

- [ ] **Step 2: 验证 init_db**

```python
import asyncio
from storage.sqlite_client import init_db, get_db

async def test():
    await init_db()
    db = await get_db()
    tables = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = [row[0] for row in await tables.fetchall()]
    assert "cache_entries" in names
    assert "request_metrics" in names
    assert "sessions" in names
    assert "turns" in names
    assert "eval_history" in names
    await db.close()
    print("OK")

asyncio.run(test())
```

---

## Phase 3: Core — Common Modules

### Task 3.1: structlog 日志配置

**Files:**
- Create: `core/logging_config.py`

**Interfaces:**
- Produces: `setup_logging()` 初始化 structlog; `get_logger()` → structlog.BoundLogger

- [ ] **Step 1: 写入 core/logging_config.py**

```python
import structlog
import logging
from core.config import ConfigRegistry

def setup_logging():
    log_level = ConfigRegistry.get("logging.level", "INFO")
    log_dir = ConfigRegistry.get("logging.log_dir", "data/logs")
    output = ConfigRegistry.get("logging.output", "file")

    processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if ConfigRegistry.get("logging.format", "json") == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

def get_logger(**kwargs):
    return structlog.get_logger(**kwargs)
```

---

### Task 3.2: 语言检测 & Chunk 标记

**Files:**
- Create: `core/bilingual.py`

**Interfaces:**
- Produces: `BilingualHandler.detect(text: str) -> str` (zh/en/mixed), `BilingualHandler.tag_chunks(chunks) -> chunks`

- [ ] **Step 1: 写入 core/bilingual.py**

```python
import re

class BilingualHandler:
    @staticmethod
    def detect(text: str) -> str:
        cn_chars = len(re.findall(r'[一-鿿]', text))
        en_chars = len(re.findall(r'[a-zA-Z]', text))
        if cn_chars == 0:
            return "en"
        if en_chars == 0:
            return "zh"
        return "mixed" if cn_chars > 0 and en_chars > 0 else ("zh" if cn_chars >= en_chars else "en")

    @staticmethod
    def tag_chunks(chunks: list) -> list:
        for c in chunks:
            if hasattr(c, 'metadata'):
                c.metadata["language"] = BilingualHandler.detect(c.text)
            elif isinstance(c, dict):
                c["language"] = BilingualHandler.detect(c.get("text", ""))
        return chunks
```

---

### Task 3.3: MetadataFilter + ExpireFilter

**Files:**
- Create: `core/metadata.py`

**Interfaces:**
- Produces: `MetadataFilter.classify(query) -> str`, `MetadataFilter.get_doc_types(category) -> list`, `ExpireFilter.get_where_clause() -> dict|None`

- [ ] **Step 1: 写入 core/metadata.py**

```python
from datetime import datetime, timedelta
from core.config import ConfigRegistry

class MetadataFilter:
    @classmethod
    def classify(cls, query: str) -> str:
        doc_types = ConfigRegistry.get("doc_types", {})
        for category, cfg in doc_types.items():
            for kw in cfg.get("keywords", []):
                if kw.lower() in query.lower():
                    return category
        return "general"

    @classmethod
    def get_doc_types(cls, category: str) -> list:
        if category == "general":
            return []
        return ConfigRegistry.get(f"doc_types.{category}.doc_type", [])

class ExpireFilter:
    def __init__(self):
        self.enabled = ConfigRegistry.get("retrieval.metadata_filter.expire.enabled", False)
        self.grace_days = ConfigRegistry.get("retrieval.metadata_filter.expire.grace_period_days", 90)

    def get_where_clause(self):
        if not self.enabled:
            return None
        cutoff = (datetime.now() - timedelta(days=self.grace_days)).strftime("%Y-%m-%d")
        return {
            "$or": [
                {"effective_date": {"$gte": cutoff}},
                {"effective_date": None}
            ]
        }
```

---

## Phase 4: Core — Ingest Pipeline

### Task 4.1: OCRPipeline

**Files:**
- Create: `core/ocr.py`

**Interfaces:**
- Produces: `OCRPipeline.process(file_path) -> ParsedDoc(text, pages, tables, language)`

- [ ] **Step 1: 写入 core/ocr.py**

```python
import fitz  # PyMuPDF
from core.config import ConfigRegistry
from core.bilingual import BilingualHandler

class ParsedDoc:
    def __init__(self, text: str, pages: list, tables: list, source: str, language: str):
        self.text = text
        self.pages = pages
        self.tables = tables
        self.source = source
        self.language = language

class OCRPipeline:
    """PDF 解析 + OCR。扫描件检测：逐页 text_ratio < 1% 视为扫描页，走 PaddleOCR。"""

    def process(self, file_path: str) -> ParsedDoc:
        doc = fitz.open(file_path)
        all_text, pages_data, tables_data = [], [], []
        need_ocr = ConfigRegistry.get("ocr.engine", "paddleocr")
        clean_cfg = ConfigRegistry.get("ocr.clean", {})

        for page_num, page in enumerate(doc):
            text = page.get_text("text")
            text_ratio = len(text.strip()) / max(page.rect.width * page.rect.height, 1)

            if text_ratio < 0.01 and need_ocr:
                text = self._ocr_page(page, page_num)
            else:
                text = self._extract_native(page)

            if clean_cfg.get("remove_header_footer", True):
                text = self._remove_header_footer(text)
            if clean_cfg.get("normalize_whitespace", True):
                text = self._normalize_whitespace(text)
            if clean_cfg.get("fix_cn_en_spacing", True):
                text = self._fix_cn_en_spacing(text)

            all_text.append(text)
            pages_data.append({"num": page_num, "text": text})

        full_text = "\n\n".join(all_text)
        language = BilingualHandler.detect(full_text)
        doc.close()
        return ParsedDoc(text=full_text, pages=pages_data, tables=tables_data, source=file_path, language=language)

    def _extract_native(self, page) -> str:
        blocks = page.get_text("blocks")
        blocks.sort(key=lambda b: (b[1], b[0]))
        return "\n".join(b[4] for b in blocks if b[4].strip())

    def _ocr_page(self, page, page_num: int) -> str:
        from paddleocr import PaddleOCR
        dpi = ConfigRegistry.get("ocr.dpi", 300)
        pix = page.get_pixmap(dpi=dpi)
        img_path = f"data/ocr/page_{page_num}.png"
        pix.save(img_path)
        ocr = PaddleOCR(lang=ConfigRegistry.get("ocr.language", ["ch", "en"]), use_angle_cls=True)
        result = ocr.ocr(img_path, cls=True)
        if result and result[0]:
            return "\n".join(line[1][0] for line in result[0])
        return ""

    def _remove_header_footer(self, text: str) -> str:
        lines = text.strip().split("\n")
        if len(lines) <= 3:
            return text
        return "\n".join(lines[1:-1])

    def _normalize_whitespace(self, text: str) -> str:
        import re
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[ \t]{2,}', ' ', text)
        return text.strip()

    def _fix_cn_en_spacing(self, text: str) -> str:
        import re
        text = re.sub(r'([一-鿿])([a-zA-Z0-9])', r'\1 \2', text)
        text = re.sub(r'([a-zA-Z0-9])([一-鿿])', r'\1 \2', text)
        return text
```

---

### Task 4.2: HierarchicalChunker

**Files:**
- Create: `core/chunker.py`

**Interfaces:**
- Consumes: `ConfigRegistry.get("chunking.*")`
- Produces: `HierarchicalChunker.chunk(ParsedDoc) -> List[Chunk]`

```python
# Chunk dataclass
@dataclass
class Chunk:
    chunk_id: str
    text: str
    heading_path: str
    metadata: dict
    embedding: Optional[List[float]] = None
    language: str = "unknown"
```

- [ ] **Step 1: 写入 core/chunker.py**（按标题层级 → 段落 → 句子三级切割 + overlap + heading_context 注入，约 80 行）

---

### Task 4.3: Embedder & Dedup & VersionedIngest

**Files:**
- Create: `core/embedder.py` — BGE-M3 批量编码
- Create: `core/dedup.py` — MD5 去重 + ConflictDetector
- Create: `core/versioned.py` — VersionedIngestService (hash+事务替换)

- [ ] **Step 1: 写入 core/embedder.py**

```python
from sentence_transformers import SentenceTransformer
from core.config import ConfigRegistry
import numpy as np

class Embedder:
    def __init__(self):
        self.model = SentenceTransformer(
            ConfigRegistry.get("embedding.model", "BAAI/bge-m3"),
            device=ConfigRegistry.get("embedding.device", "mps")
        )
        self.batch_size = ConfigRegistry.get("embedding.batch_size", 32)

    def encode(self, texts: list) -> np.ndarray:
        return self.model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                                 show_progress_bar=False)
```

- [ ] **Step 2: 写入 core/dedup.py** — MD5 exact-match + conflict detection（~50 行，设计文档 §5.3）

- [ ] **Step 3: 写入 core/versioned.py** — VersionedIngest（hash 去重 + 事务替换 + 写 ChromaDB，~60 行，设计文档 §5.4）

---

## Phase 5: Core — Query Pipeline

### Task 5.1: RRF Fusion

**Files:**
- Create: `core/fusion.py`

**Interfaces:**
- Produces: `RRFFusion.fuse(results_A, results_B, k=60) -> List[ScoredDoc]`

- [ ] **Step 1: 写入 core/fusion.py**

```python
class RRFFusion:
    @staticmethod
    def fuse(vec_results: list, bm25_results: list, k: int = 60) -> list:
        scores = {}
        for rank, doc in enumerate(vec_results):
            chunk_id = doc.get("chunk_id", id(doc))
            scores[chunk_id] = scores.get(chunk_id, 0) + 1.0 / (k + rank + 1)
            if isinstance(doc, dict):
                scores[chunk_id + "_data"] = doc
        for rank, doc in enumerate(bm25_results):
            chunk_id = doc.get("chunk_id", id(doc))
            scores[chunk_id] = scores.get(chunk_id, 0) + 1.0 / (k + rank + 1)
            if isinstance(doc, dict) and (chunk_id + "_data") not in scores:
                scores[chunk_id + "_data"] = doc

        scored = [(cid, s) for cid, s in scores.items() if not cid.endswith("_data")]
        scored.sort(key=lambda x: x[1], reverse=True)
        result = []
        seen = set()
        for cid, rrf_score in scored:
            if cid not in seen:
                seen.add(cid)
                data = scores.get(cid + "_data", {})
                data["rrf_score"] = rrf_score
                result.append(data)
        return result
```

---

### Task 5.2: Retrieval 模块 (Vector + BM25 + Hybrid)

**Files:**
- Create: `core/retriever.py`

**Interfaces:**
- Consumes: `ChromaStore`, `ConfigRegistry`
- Produces: `Retriever.retrieve(query, top_k, doc_type_filter) -> RetrievalResult`

- [ ] **Step 1: 写入 core/retriever.py** — 包含 `ScoredDoc`, `RetrievalResult`, `VectorRetriever`, `BM25Retriever`, `HybridRetriever`, `RerankedRetriever`, `AdaptiveK`（~120 行，设计文档 §4.3）

---

### Task 5.3: Reranker + CircuitBreaker

**Files:**
- Create: `core/reranker.py`

**Interfaces:**
- Consumes: `ConfigRegistry.get("reranker.*")`
- Produces: `Reranker.rerank(query, candidates) -> List[ScoredDoc]`

- [ ] **Step 1: 写入 core/reranker.py** — Cross-Encoder + `RerankerCircuitBreaker`（~60 行，设计文档 §4.4）

---

### Task 5.4: InjectionScanner

**Files:**
- Create: `core/scanner.py`

**Interfaces:**
- Produces: `InjectionScanner.scan(chunks) -> (cleaned_chunks, blocked_count)`

- [ ] **Step 1: 写入 core/scanner.py** — 注入模式匹配 + block/warn/allow（~50 行，设计文档 §6.1）

---

### Task 5.5: Generator (Adapter + Prompt)

**Files:**
- Create: `core/generator.py` — BaseLLMAdapter ABC + DeepSeek/Qwen/GLM adapters
- Create: `core/prompt.py` — PromptBuilder + PromptFencer

**Interfaces:**
- Consumes: `ConfigRegistry.get("llm.*")`
- Produces: `Generator.generate(prompt_context) -> GenerationResult`, `PromptBuilder.build(ctx) -> List[dict]`

- [ ] **Step 1: 写入 core/generator.py**（Adapter 模式，~80 行，设计文档 §4.5）

- [ ] **Step 2: 写入 core/prompt.py** — System prompt + XML 沙箱模板（~60 行，设计文档 §4.5）

---

### Task 5.6: PostProcessor (PII + Refusal)

**Files:**
- Create: `core/postprocess.py`

**Interfaces:**
- Produces: `PostProcessor.process(answer, retrieval_result, query) -> (answer, refused, refusal_reason)`

- [ ] **Step 1: 写入 core/postprocess.py** — PIIScrubber + RefusalCheck（~80 行，设计文档 §4.6）

---

## Phase 6: Core — Cross-cutting

### Task 6.1: CacheManager

**Files:**
- Create: `core/cache.py`

**Interfaces:**
- Produces: `CacheManager.get(query, mode) -> CachedAnswer|None`, `CacheManager.put(query, mode, answer, sources)`

- [ ] **Step 1: 写入 core/cache.py** — L1 SQLite exact-match + TTL 过期 + LRU + L2 预留（~70 行，设计文档 §4.7）

---

### Task 6.2: ResilienceGuard

**Files:**
- Create: `core/guard.py`

**Interfaces:**
- Produces: `ResilienceGuard.acquire()`, `ResilienceGuard.with_stage_timeout(stage, coro)`, `ResilienceGuard.with_request_timeout(coro)`

- [ ] **Step 1: 写入 core/guard.py** — Semaphore(10) + 阶段超时 + 请求硬超时（~60 行，设计文档 §4.8）

---

### Task 6.3: ConversationCompressor

**Files:**
- Create: `core/compressor.py`

**Interfaces:**
- Produces: `ConversationCompressor.compress(turns) -> CompressedHistory`

- [ ] **Step 1: 写入 core/compressor.py** — 保留最近 3 轮 + 早期轮次 LLM 摘要（~40 行，设计文档 §4.9）

---

## Phase 7: Service Layer

### Task 7.1: RetrievalService

**Files:**
- Create: `services/__init__.py` (empty)
- Create: `services/retrieval.py`

**Interfaces:**
- Consumes: `Retriever`, `Reranker`, `InjectionScanner`, `ResilienceGuard`, `ConfigRegistry`
- Produces: `RetrievalService.retrieve(query, doc_type) -> RetrievalOutput`

- [ ] **Step 1: 写入 services/retrieval.py** — 编排 Retriever → Reranker → AdaptiveK → Scanner，含超时降级（~50 行，设计文档 §4.2）

---

### Task 7.2: ChatService

**Files:**
- Create: `services/chat.py`

**Interfaces:**
- Consumes: `RetrievalService`, `CacheManager`, `Generator`, `PostProcessor`, `ResilienceGuard`, `ContextBuilder`
- Produces: `ChatService.process(query, session_id) -> ChatResponse`

- [ ] **Step 1: 写入 services/chat.py** — 顶层编排：缓存 → 预处理 → 检索 → 生成 → 后处理 → 持久化（~80 行，设计文档 §4.1）

---

### Task 7.3: IngestService

**Files:**
- Create: `services/ingest.py`

**Interfaces:**
- Consumes: `OCRPipeline`, `HierarchicalChunker`, `Embedder`, `DedupPipeline`, `VersionedIngest`, `BilingualHandler`, `ConflictDetector`
- Produces: `IngestService.ingest(file_path, doc_type, version) -> IngestResult`

- [ ] **Step 1: 写入 services/ingest.py** — OCR → 分片 → 去重 → 向量化 → 版本化入库 → 冲突检测（~60 行，设计文档 §5 概述）

---

## Phase 8: API Layer

### Task 8.1: API Schemas & Routes

**Files:**
- Create: `api/__init__.py` (empty)
- Create: `api/schemas.py`
- Create: `api/routes.py`
- Create: `api/app.py`

**Interfaces:**
- Consumes: `ChatService`, `IngestService`, `EvalService`

- [ ] **Step 1: 写入 api/schemas.py** — ChatRequest/Response, IngestResponse, EvalRequest/Response, HealthResponse

- [ ] **Step 2: 写入 api/routes.py** — POST /chat, POST /ingest, POST /eval/run, GET /eval/result, GET /report, GET /health

- [ ] **Step 3: 写入 api/app.py** — FastAPI 应用入口，启动时 init_db + ConfigRegistry.init + setup_logging

---

## Phase 9: Evaluation Pipeline

### Task 9.1: EvalService + Runner + Report

**Files:**
- Create: `eval/__init__.py` (empty)
- Create: `eval/test_set.py` — QA test set loader
- Create: `eval/runner.py` — 三配置对比 + Ragas 执行
- Create: `eval/report.py` — CSV/MD 生成 + compare_runs()

**Interfaces:**
- Consumes: `ChatService`, `EvalService`, `eval_history` SQLite table, `Ragas`
- Produces: `eval.sh` 执行 → data/eval/results/report.csv

- [ ] **Step 1: 写入 eval/test_set.py** — 加载 50 QA pairs JSON（LLM 生成 + 人工修正），格式: `[{question, ground_truth, relevant_chunks, language, is_out_of_scope}]`

- [ ] **Step 2: 写入 eval/runner.py** — 三配置循环 + Ragas metrics + 写入 eval_history（~100 行）

- [ ] **Step 3: 写入 eval/report.py** — CSV/MD 生成 + `compare_runs(before_id, after_id)`（~60 行）

---

## Phase 10: Scripts & Docs

### Task 10.1: run.sh & eval.sh

**Files:**
- Create: `run.sh`
- Create: `eval.sh`

- [ ] **Step 1: 写入 run.sh**

```bash
#!/bin/bash
set -e
pip install -r requirements.txt
mkdir -p data/{corpus,chroma,ocr,logs,eval/results}
python -m api.app
```

- [ ] **Step 2: 写入 eval.sh**

```bash
#!/bin/bash
set -e
python -m eval.runner --output data/eval/results/
echo "评估完成: data/eval/results/report.csv"
```

- [ ] **Step 3: 验证启动**

```bash
chmod +x run.sh eval.sh
python -c "import api.app; print('API module OK')"
```

---

### Task 10.2: README.md

**Files:**
- Create: `README.md`

- [ ] **Step 1: 写入 README.md** — 快速上手（安装→启动→评测）+ 架构简图 + 配置说明 + 端点文档链接

---

## Task 依赖关系

```
Phase 1 (骨架+配置)
  └─→ Phase 2 (存储层)
       └─→ Phase 3 (通用模块: logging, bilingual, metadata)
            └─→ Phase 4 (Ingest Pipeline: OCR→Chunker→Embedder→Dedup→Versioned)
                 └─→ Phase 5 (Query Pipeline: Fusion→Retriever→Reranker→Scanner→Generator→PostProc)
                      └─→ Phase 6 (横切: Cache→Guard→Compressor)
                           └─→ Phase 7 (Service: RetrievalService→ChatService→IngestService)
                                └─→ Phase 8 (API: Schemas→Routes→App)
                                     └─→ Phase 9 (Eval: TestSet→Runner→Report)
                                          └─→ Phase 10 (Scripts+Docs)
```

---

> **下一步：** 使用 superpowers:subagent-driven-development 逐 Task 实施，或使用 superpowers:executing-plans 批量执行。
