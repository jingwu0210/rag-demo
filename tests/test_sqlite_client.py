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


def test_init_db_turns_source_column_migration(tmp_path):
    """旧库 turns 无 source 列 → init_db ALTER 补列（DEFAULT 'chat'）；存量行默认 'chat'；幂等

    与 request_metrics 的 source 迁移同款：Database 次级菜单按 source 过滤依赖该列，
    旧库升级时存量 turn 一律归入 chat 组。
    """
    from core.config import ConfigRegistry
    from storage.sqlite_client import init_db

    ConfigRegistry.init("config.yaml")
    db_path = os.path.join(tmp_path, "old_turns.db")
    ConfigRegistry.override("paths.sqlite", db_path)

    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE turns (
            turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL, raw_query TEXT NOT NULL,
            answer TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "INSERT INTO turns (turn_id, session_id, turn_index, raw_query, answer) "
        "VALUES ('t-old', 's-old', 0, '旧问题', '旧答案')")
    conn.commit()
    conn.close()

    asyncio.run(_run_init())          # 触发 ALTER 迁移（列已存在则忽略，幂等）

    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
    assert "source" in cols, f"turns 缺少 source 列: {cols}"
    row = conn.execute(
        "SELECT source FROM turns WHERE turn_id = 't-old'").fetchone()
    assert row[0] == "chat"           # 存量行默认 chat
    conn.close()

    # 幂等：再次 init_db 不报错
    asyncio.run(_run_init())
