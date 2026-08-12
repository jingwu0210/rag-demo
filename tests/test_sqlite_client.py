import asyncio
import os
import tempfile

def test_init_db_creates_all_tables():
    from core.config import ConfigRegistry
    from storage import sqlite_client

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        ConfigRegistry.init("config.yaml")
        ConfigRegistry.override("paths.sqlite", db_path)

        asyncio.run(_run_init())
        assert os.path.exists(db_path)

        # 用同步 sqlite3 验证表存在（避免 asyncio 事件循环问题）
        import sqlite3
        conn = sqlite3.connect(db_path)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {"cache_entries", "request_metrics", "sessions", "turns", "ingest_log", "eval_history"}
        assert expected.issubset(tables), f"missing: {expected - tables}"
        conn.close()

async def _run_init():
    from storage.sqlite_client import init_db
    await init_db()
