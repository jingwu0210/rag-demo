"""一次性迁移脚本：存量 request_metrics / turns 的 source 由 'chat' 近似归为 'eval'。

背景：source 列是后加的，R1-R7 评估 + demo 聊天的存量数据在 init_db ALTER 补列时
默认归 'chat'（约 330 条评估 + 约 10 条 chat）。本脚本把 source='chat' 的存量行
近似全量归为 'eval'——近似而非精确：无法从存量数据追溯每条记录的真实来源。

用法:
  .venv/bin/python scripts/migrate_source_eval.py

安全设计（铁律 10/12：不 commit、不动 init_db、不误伤新数据）：
  - 独立脚本手动运行，init_db 不碰存量数据（避免每次启动把新写入的 chat 数据误归 eval）
  - 幂等：用 _migration_flags 表记录"已执行"标记，重复运行直接跳过——
    不依赖"是否存在 source='chat' 行"做判断（迁移后新对话也是 chat，会误判）
  - 只改 request_metrics / turns 两表的 source 列；cache_entries/sessions/ingest_log/
    eval_history 均不受影响
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import ConfigRegistry

# 迁移标记 key（一次性；变更迁移语义时换新 key）
MIGRATION_KEY = "source_chat_to_eval_20260815"

_MIGRATION_FLAGS_DDL = """
    CREATE TABLE IF NOT EXISTS _migration_flags (
        key TEXT PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
"""


def migrate(db_path: str):
    """对指定 sqlite 库执行存量 source 迁移（chat→eval）。幂等。

    返回：
      - "skip-no-db"      数据库文件不存在（无需迁移）
      - "already-applied" 已执行过（标记存在），跳过
      - {"metrics": n, "turns": n}  本次迁移的行数
    """
    if not os.path.exists(db_path):
        return "skip-no-db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_MIGRATION_FLAGS_DDL)
        cur = conn.execute(
            "SELECT 1 FROM _migration_flags WHERE key = ?", (MIGRATION_KEY,))
        if cur.fetchone():
            return "already-applied"
        n_metrics = conn.execute(
            "SELECT COUNT(*) FROM request_metrics WHERE source = 'chat'").fetchone()[0]
        n_turns = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE source = 'chat'").fetchone()[0]
        conn.execute("UPDATE request_metrics SET source = 'eval' WHERE source = 'chat'")
        conn.execute("UPDATE turns SET source = 'eval' WHERE source = 'chat'")
        conn.execute("INSERT INTO _migration_flags (key) VALUES (?)", (MIGRATION_KEY,))
        conn.commit()
        return {"metrics": n_metrics, "turns": n_turns}
    finally:
        conn.close()


def main():
    ConfigRegistry.init("config.yaml")
    db_path = ConfigRegistry.get("paths.sqlite", "workspace/cache.db")
    print(f"目标库: {db_path}")
    result = migrate(db_path)
    if result == "skip-no-db":
        print("数据库文件不存在，无需迁移。")
        return
    if result == "already-applied":
        print(f"迁移已执行过（标记 {MIGRATION_KEY}），跳过。")
        return
    n_metrics, n_turns = result["metrics"], result["turns"]
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] 迁移完成：request_metrics {n_metrics} 行、turns {n_turns} 行 source 'chat'→'eval'。")
    print("注：存量近似归 eval，非精确区分——无法追溯每条记录的真实来源（评估/用户对话）。")


if __name__ == "__main__":
    main()
