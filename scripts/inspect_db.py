"""数据库查看工具：SQLite 各表的查询入口。

用法:
  .venv/bin/python scripts/inspect_db.py                          # eval 汇总（默认）
  .venv/bin/python scripts/inspect_db.py eval --run <run_id>      # 指定 run 汇总
  .venv/bin/python scripts/inspect_db.py eval --bad               # 异常样本
  .venv/bin/python scripts/inspect_db.py eval --runs              # 所有历史评估
  .venv/bin/python scripts/inspect_db.py ingest                   # 入库日志
  .venv/bin/python scripts/inspect_db.py metrics                  # 请求指标（最近 20 条）
  .venv/bin/python scripts/inspect_db.py turns --session <id>     # 对话轮次

数据位置: data/cache.db
  - eval_history: 每 (run_id, config_name) 一行 = 一次评估一个配置的聚合指标；
    per_qa_results_json 列 = 该配置下每条 QA 的明细（JSON 数组）
  - ingest_log: 每次文档入库一行（status = ingested/replaced/skipped）
  - request_metrics: 每次 /chat 请求一行（延迟/token/拒答/安全计数）
  - turns: 每轮对话一行（query/answer/sources/timing）
"""
import argparse
import json
import sqlite3
import sys

DB = "data/cache.db"


def _rows(run_id: str = None):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    if run_id:
        rows = conn.execute(
            "SELECT * FROM eval_history WHERE run_id = ? ORDER BY config_name",
            (run_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM eval_history ORDER BY timestamp DESC, config_name "
            "LIMIT 3").fetchall()
    conn.close()
    return rows


def show_summary(run_id: str = None):
    rows = _rows(run_id)
    if not rows:
        print("eval_history 为空（尚未跑过评估，或数据被清理）。运行 eval.sh 生成。")
        return
    print(f"{'run_id':<20} {'config':<15} {'faith':<7} {'CP':<7} {'compl':<7} "
          f"{'style':<7} {'refusal':<8} {'timeout':<8} {'p50':<6} {'p95':<6}")
    print("-" * 95)
    for r in rows:
        print(f"{r['run_id'][-20:]:<20} {r['config_name']:<15} "
              f"{_fmt(r['faithfulness']):<7} {_fmt(r['context_precision']):<7} "
              f"{_fmt(r['answer_compliance']):<7} {_fmt(r['style_consistency']):<7} "
              f"{_fmt(r['refusal_appropriateness']):<8} {_fmt(r['timeout_rate']):<8} "
              f"{r['p50_latency_ms']:<6} {r['p95_latency_ms']:<6}")


def show_per_qa(run_id: str = None, bad_only: bool = False):
    rows = _rows(run_id)
    if not rows:
        print("eval_history 为空。运行 eval.sh 生成。")
        return
    for r in rows:
        per_qa = json.loads(r["per_qa_results_json"] or "[]")
        print(f"\n=== {r['run_id'][-20:]} / {r['config_name']} "
              f"({len(per_qa)} 条) ===")
        for q in per_qa:
            is_bad = (q.get("timeout")
                      or (q.get("faithfulness") is not None and q["faithfulness"] < 0.5)
                      or (q.get("refusal_appropriateness") == 0)
                      or (q.get("answer_compliance") is not None
                          and q["answer_compliance"] <= 3))
            if bad_only and not is_bad:
                continue
            flag = " ⚠️" if is_bad else ""
            print(f"\n[{r['config_name']}] Q: {q['question'][:60]}{flag}")
            print(f"  answer: {q['answer'][:100]}")
            print(f"  faith={_fmt(q.get('faithfulness'))} "
                  f"cp={_fmt(q.get('context_precision'))} "
                  f"compliance={q.get('answer_compliance')} "
                  f"refused={q.get('refused')} timeout={q.get('timeout')} "
                  f"src={q.get('sources_count')} tokens={q.get('tokens_total')}")


def show_runs():
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT run_id, timestamp, test_set_hash, COUNT(*) AS configs "
        "FROM eval_history GROUP BY run_id ORDER BY timestamp DESC").fetchall()
    conn.close()
    if not rows:
        print("eval_history 为空。运行 eval.sh 生成。")
        return
    print(f"{'run_id':<32} {'timestamp':<20} {'configs':<8} {'test_set_hash'}")
    for r in rows:
        print(f"{r[0]:<32} {r[1]:<20} {r[3]:<8} {r[2][:16]}")


def _fmt(v):
    return "—" if v is None else f"{v:.4f}"


# ── ingest_log 查看 ────────────────────────────────────────

def show_ingest(limit: int = 20):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM ingest_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    if not rows:
        print("ingest_log 为空。运行 scripts/ingest_corpus.py 或 POST /ingest 生成。")
        return
    print(f"{'时间':<20} {'状态':<10} {'文档':<32} {'版本':<8} "
          f"{'新建':<5} {'替换':<5}")
    print("-" * 90)
    for r in reversed(rows):
        print(f"{r['created_at'][:19]:<20} {r['status']:<10} "
              f"{r['source_file']:<32} {r['version'] or '—':<8} "
              f"{r['chunks_created'] or 0:<5} {r['chunks_replaced'] or 0:<5}")


# ── request_metrics 查看 ───────────────────────────────────

def show_metrics(limit: int = 20):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM request_metrics ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    if not rows:
        print("request_metrics 为空。发一次 /chat 请求生成。")
        return
    print(f"{'时间':<20} {'模式':<15} {'总延迟':<7} {'token':<7} "
          f"{'缓存':<5} {'拒答':<5} {'超时':<5} {'PII':<4} {'注入':<4}")
    print("-" * 90)
    for r in reversed(rows):
        print(f"{r['timestamp'][:19]:<20} {r['retrieval_mode'] or '—':<15} "
              f"{r['latency_total'] or 0:<7} {r['token_total'] or 0:<7} "
              f"{'✓' if r['cache_hit'] else '—':<5} "
              f"{'✓' if r['refused'] else '—':<5} "
              f"{'✓' if r['timeout'] else '—':<5} "
              f"{r['pii_redact_count'] or 0:<4} {r['injection_blocked'] or 0:<4}")


# ── turns 查看 ─────────────────────────────────────────────

def show_turns(session_id: str = None, limit: int = 20):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    if session_id:
        rows = conn.execute(
            "SELECT * FROM turns WHERE session_id = ? ORDER BY turn_index",
            (session_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM turns ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    if not rows:
        print("turns 为空。发一次 /chat 请求生成。")
        return
    for r in rows:
        print(f"\n[{r['session_id'][:8]}] turn#{r['turn_index']} "
              f"{r['created_at'][:19]}  mode={r['retrieval_mode'] or '—'} "
              f"tokens={r['token_total'] or 0}")
        print(f"  Q: {r['raw_query'][:80]}")
        print(f"  A: {r['answer'][:120] if r['answer'] else '(拒答)'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="数据库查看工具（eval/ingest/metrics/turns）")
    sub = parser.add_subparsers(dest="command")
    p_eval = sub.add_parser("eval", help="评估数据（默认）")
    p_eval.add_argument("--run", help="指定 run_id")
    p_eval.add_argument("--bad", action="store_true", help="只显示异常样本")
    p_eval.add_argument("--runs", action="store_true", help="列出所有历史评估")
    p_ingest = sub.add_parser("ingest", help="入库日志")
    p_ingest.add_argument("--limit", type=int, default=20)
    p_metrics = sub.add_parser("metrics", help="请求指标")
    p_metrics.add_argument("--limit", type=int, default=20)
    p_turns = sub.add_parser("turns", help="对话轮次")
    p_turns.add_argument("--session", help="指定 session_id")
    p_turns.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    if args.command == "ingest":
        show_ingest(args.limit)
    elif args.command == "metrics":
        show_metrics(args.limit)
    elif args.command == "turns":
        show_turns(args.session, args.limit)
    elif args.command == "eval" or args.command is None:
        if args.command is not None and args.runs:
            show_runs()
        elif args.command is not None and (args.bad or args.run):
            show_per_qa(args.run, bad_only=args.bad)
        else:
            show_summary(args.run if args.command else None)
