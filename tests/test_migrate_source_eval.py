"""scripts/migrate_source_eval.py 迁移逻辑测试

- migrate() 为独立函数，可导入直测（不依赖脚本 main 的 ConfigRegistry）
- 覆盖：chat→eval 归并、eval 行不动、幂等（标记跳过，不误伤迁移后的新 chat 行）、
  库不存在跳过
"""
import os
import sqlite3

from scripts.migrate_source_eval import MIGRATION_KEY, migrate


def _make_db(path):
    """构造含 request_metrics / turns 的库（含 source 列），插入 chat/eval 混合行"""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE request_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
            latency_total INTEGER, token_total INTEGER, source TEXT DEFAULT 'chat'
        )
    """)
    conn.execute("""
        CREATE TABLE turns (
            turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL, raw_query TEXT NOT NULL,
            source TEXT DEFAULT 'chat'
        )
    """)
    # request_metrics：3 chat + 1 eval
    for i in range(3):
        conn.execute(
            "INSERT INTO request_metrics (request_id, latency_total, token_total, source) "
            "VALUES (?, 10, 5, 'chat')", (f"r-chat-{i}",))
    conn.execute(
        "INSERT INTO request_metrics (request_id, latency_total, token_total, source) "
        "VALUES ('r-eval', 10, 5, 'eval')")
    # turns：2 chat + 1 eval（依赖 sessions 外键——这里不建 sessions，直接插入无外键约束）
    for i in range(2):
        conn.execute(
            "INSERT INTO turns (turn_id, session_id, turn_index, raw_query, source) "
            "VALUES (?, 's', ?, 'q', 'chat')", (f"t-chat-{i}", i))
    conn.execute(
        "INSERT INTO turns (turn_id, session_id, turn_index, raw_query, source) "
        "VALUES ('t-eval', 's', 9, 'q', 'eval')")
    conn.commit()
    conn.close()


def test_migrate_moves_chat_to_eval(tmp_path):
    """存量 chat 行归 eval；eval 行不动；返回迁移行数"""
    db_path = os.path.join(tmp_path, "cache.db")
    _make_db(db_path)

    result = migrate(db_path)

    assert result == {"metrics": 3, "turns": 2}
    conn = sqlite3.connect(db_path)
    try:
        n_metrics_eval = conn.execute(
            "SELECT COUNT(*) FROM request_metrics WHERE source = 'eval'").fetchone()[0]
        n_metrics_chat = conn.execute(
            "SELECT COUNT(*) FROM request_metrics WHERE source = 'chat'").fetchone()[0]
        n_turns_eval = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE source = 'eval'").fetchone()[0]
        n_turns_chat = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE source = 'chat'").fetchone()[0]
        marker = conn.execute(
            "SELECT 1 FROM _migration_flags WHERE key = ?", (MIGRATION_KEY,)).fetchone()
    finally:
        conn.close()
    assert n_metrics_eval == 4        # 3 chat→eval + 原 1 eval
    assert n_metrics_chat == 0
    assert n_turns_eval == 3          # 2 chat→eval + 原 1 eval
    assert n_turns_chat == 0
    assert marker is not None         # 标记已写


def test_migrate_idempotent_no_harm_to_new_chat(tmp_path):
    """幂等：二次迁移跳过（already-applied）；迁移后新写入的 chat 行不被误归 eval"""
    db_path = os.path.join(tmp_path, "cache.db")
    _make_db(db_path)
    migrate(db_path)

    # 迁移后新写入 1 条 chat（模拟新的用户对话）
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO request_metrics (request_id, latency_total, token_total, source) "
        "VALUES ('r-new-chat', 20, 9, 'chat')")
    conn.execute(
        "INSERT INTO turns (turn_id, session_id, turn_index, raw_query, source) "
        "VALUES ('t-new-chat', 's', 10, 'q', 'chat')")
    conn.commit()
    conn.close()

    result = migrate(db_path)

    assert result == "already-applied"
    conn = sqlite3.connect(db_path)
    try:
        n_chat = conn.execute(
            "SELECT COUNT(*) FROM request_metrics WHERE source = 'chat'").fetchone()[0]
        n_turn_chat = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE source = 'chat'").fetchone()[0]
    finally:
        conn.close()
    assert n_chat == 1                # 新 chat 行保持 chat，未被误归 eval
    assert n_turn_chat == 1


def test_migrate_skip_no_db(tmp_path):
    """数据库文件不存在 → skip-no-db"""
    assert migrate(os.path.join(tmp_path, "nonexistent.db")) == "skip-no-db"
