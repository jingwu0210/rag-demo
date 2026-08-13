"""评估数据查看工具：eval_history 汇总 + per_qa 明细。

用法:
  .venv/bin/python scripts/inspect_eval.py                    # 最近一次评估的汇总
  .venv/bin/python scripts/inspect_eval.py --run <run_id>     # 指定 run
  .venv/bin/python scripts/inspect_eval.py --bad              # 列出低分/超时/拒答异常样本
  .venv/bin/python scripts/inspect_eval.py --runs             # 列出所有历史评估

数据位置: data/cache.db 的 eval_history 表
  - 每 (run_id, config_name) 一行 = 一次评估一个检索配置的聚合指标
  - per_qa_results_json 列 = 该配置下每条 QA 的明细（JSON 数组）
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="评估数据查看")
    parser.add_argument("--run", help="指定 run_id")
    parser.add_argument("--bad", action="store_true", help="只显示异常样本")
    parser.add_argument("--runs", action="store_true", help="列出所有历史评估")
    args = parser.parse_args()
    if args.runs:
        show_runs()
    elif args.bad or args.run:
        show_per_qa(args.run, bad_only=args.bad)
    else:
        show_summary(args.run)
