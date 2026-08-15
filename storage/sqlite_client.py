import aiosqlite
from core.config import ConfigRegistry

async def get_db() -> aiosqlite.Connection:
    db_path = ConfigRegistry.get("paths.sqlite", "workspace/cache.db")
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    return db

async def init_db() -> None:
    db = await get_db()

    await db.execute("""
        CREATE TABLE IF NOT EXISTS cache_entries (
            cache_key TEXT PRIMARY KEY, query TEXT NOT NULL, answer TEXT NOT NULL,
            sources_json TEXT, token_usage INTEGER, retrieval_mode TEXT,
            refused BOOLEAN DEFAULT FALSE, refusal_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_cache_created ON cache_entries(created_at)")
    # 迁移：旧库（无 refused/refusal_reason 列）补列，幂等
    try:
        await db.execute("ALTER TABLE cache_entries ADD COLUMN refused BOOLEAN DEFAULT FALSE")
        await db.execute("ALTER TABLE cache_entries ADD COLUMN refusal_reason TEXT")
    except Exception:
        pass  # 列已存在（新库 CREATE 已含）

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
            faithfulness_score REAL, context_precision REAL, answer_compliance REAL,
            source TEXT DEFAULT 'chat'
        )
    """)
    # 迁移：旧库（无 source 列）补列 — 存量行默认 'chat'（chat/eval 分组报表依赖），幂等
    try:
        await db.execute(
            "ALTER TABLE request_metrics ADD COLUMN source TEXT DEFAULT 'chat'")
    except Exception:
        pass  # 列已存在（新库 CREATE 已含）
    await db.execute("CREATE INDEX IF NOT EXISTS idx_metrics_ts ON request_metrics(timestamp)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_metrics_mode ON request_metrics(retrieval_mode)")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'active', turn_count INTEGER DEFAULT 0
        )
    """)

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

    await db.execute("""
        CREATE TABLE IF NOT EXISTS ingest_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL, doc_type TEXT, version TEXT,
            doc_hash TEXT, chunks_created INTEGER, chunks_replaced INTEGER,
            status TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS eval_history (
            run_id TEXT NOT NULL, config_name TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, test_set_hash TEXT NOT NULL,
            total_qa_pairs INTEGER,
            faithfulness REAL, context_precision REAL, context_recall REAL, answer_relevancy REAL,
            answer_compliance REAL, style_consistency REAL, refusal_appropriateness REAL,
            p50_latency_ms INTEGER, p95_latency_ms INTEGER, avg_tokens_per_call INTEGER,
            total_pii_redactions INTEGER DEFAULT 0, total_injections_blocked INTEGER DEFAULT 0,
            timeout_rate REAL DEFAULT 0,
            avg_prompt_tokens INTEGER DEFAULT 0,
            avg_completion_tokens INTEGER DEFAULT 0,
            avg_chunks_per_call REAL DEFAULT 0,
            unanswered_rate REAL DEFAULT 0,
            per_qa_results_json TEXT,
            PRIMARY KEY (run_id, config_name)
        )
    """)
    for col_ddl in ("ALTER TABLE eval_history ADD COLUMN timeout_rate REAL DEFAULT 0",
                    "ALTER TABLE eval_history ADD COLUMN unanswered_rate REAL DEFAULT 0",
                    "ALTER TABLE eval_history ADD COLUMN avg_prompt_tokens INTEGER DEFAULT 0",
                    "ALTER TABLE eval_history ADD COLUMN avg_completion_tokens INTEGER DEFAULT 0",
                    "ALTER TABLE eval_history ADD COLUMN avg_chunks_per_call REAL DEFAULT 0"):
        try:
            await db.execute(col_ddl)
        except Exception:
            pass  # 列已存在
    await db.execute("CREATE INDEX IF NOT EXISTS idx_eval_history_ts ON eval_history(timestamp)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_eval_history_config ON eval_history(config_name)")

    await db.commit()
    await db.close()
