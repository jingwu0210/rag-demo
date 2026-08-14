"""Evaluation — 报表生成 + before/after 对比

generate_report(results, output_dir)：
- 写 CSV（config,faithfulness,context_precision,answer_compliance,refusal_appropriateness,
  style_consistency,p50_ms,p95_ms,avg_tokens,total_requests）
- 写 Markdown（对比表格 + 结论段：哪个配置在哪个指标最优）
- 返回 CSV 路径

compare_runs(run_id_before, run_id_after)：
- 从 eval_history 拉两次运行的记录（按 config_name 对齐）
- 每指标 delta_pct = (after - before) / before * 100（before 缺失/为 0 → None）
- 测试集 hash 不同 → ValueError("test set mismatch")
"""
from __future__ import annotations

import asyncio
import csv
import os
import time
from typing import List, Optional

from core.config import ConfigRegistry
from core.logging_config import get_logger
from storage.sqlite_client import get_db

logger = get_logger(module="eval.report")

# CSV 列（brief 精确指定）
CSV_COLUMNS = ["config", "faithfulness", "context_precision", "answer_compliance",
               "refusal_appropriateness", "style_consistency", "p50_ms", "p95_ms",
               "avg_tokens", "avg_prompt_tokens", "avg_completion_tokens",
               "avg_chunks", "timeout_rate", "unanswered_rate",
               "oos_refusal_rate", "normal_refusal_rate", "total_requests"]

# CSV 列 → results dict 键（run_comparison 聚合结果）
_COLUMN_KEY = {
    "config": "config_name",
    "faithfulness": "faithfulness",
    "context_precision": "context_precision",
    "answer_compliance": "answer_compliance",
    "refusal_appropriateness": "refusal_appropriateness",
    "style_consistency": "style_consistency",
    "p50_ms": "p50_latency_ms",
    "p95_ms": "p95_latency_ms",
    "avg_tokens": "avg_tokens_per_call",
    "avg_prompt_tokens": "avg_prompt_tokens",
    "avg_completion_tokens": "avg_completion_tokens",
    "avg_chunks": "avg_chunks_per_call",
    "timeout_rate": "timeout_rate",
    "unanswered_rate": "unanswered_rate",
    "oos_refusal_rate": "oos_refusal_rate",
    "normal_refusal_rate": "normal_refusal_rate",
    "total_requests": "total_requests",
}

# compare_runs 对比的指标列（eval_history 列名）
_COMPARED_METRICS = ["faithfulness", "context_precision", "answer_compliance",
                     "refusal_appropriateness", "style_consistency",
                     "p50_latency_ms", "p95_latency_ms", "avg_tokens_per_call"]

# 指标方向：higher = 越大越好，lower = 越小越好（用于结论段选最优配置）
_METRIC_DIRECTION = {
    "faithfulness": "higher", "context_precision": "higher",
    "answer_compliance": "higher", "refusal_appropriateness": "higher",
    "style_consistency": "higher",
    "p50_latency_ms": "lower", "p95_latency_ms": "lower",
    "avg_tokens_per_call": "lower",
    "avg_prompt_tokens": "lower", "avg_completion_tokens": "lower",
    "avg_chunks_per_call": "lower", "timeout_rate": "lower",
    "unanswered_rate": "lower",
}

_METRIC_LABEL = {
    "faithfulness": "Faithfulness", "context_precision": "Context Precision",
    "answer_compliance": "Answer Compliance", "refusal_appropriateness": "Refusal Appropriateness",
    "style_consistency": "Style Consistency",
    "p50_latency_ms": "P50 Latency (ms)", "p95_latency_ms": "P95 Latency (ms)",
    "avg_tokens_per_call": "Avg Tokens/Call",
    "avg_prompt_tokens": "Avg Prompt Tokens",
    "avg_completion_tokens": "Avg Completion Tokens",
    "avg_chunks_per_call": "Avg Chunks/Call",
    "timeout_rate": "Timeout Rate",
    "unanswered_rate": "Unanswered Rate",
}


def generate_report(results: List[dict], output_dir: str) -> str:
    """写 CSV + Markdown 报告到 output_dir，返回 CSV 路径。

    文件名带时间戳（历史评估报表不互相覆盖 — before/after 对比依赖历史报表），
    同时维护 eval_report.csv/md 作为最新副本。
    """
    os.makedirs(output_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(output_dir, f"eval_report-{ts}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for r in results:
            writer.writerow([r.get(_COLUMN_KEY[col], "") for col in CSV_COLUMNS])

    md_path = os.path.join(output_dir, f"eval_report-{ts}.md")
    _write_markdown(results, md_path)

    # 最新副本（固定文件名，方便查看；历史版本在带时间戳文件中）
    import shutil
    shutil.copy2(csv_path, os.path.join(output_dir, "eval_report.csv"))
    shutil.copy2(md_path, os.path.join(output_dir, "eval_report.md"))

    logger.info("eval_report_written", csv_path=csv_path, md_path=md_path,
                configs=len(results))
    return csv_path


def _write_markdown(results: List[dict], md_path: str) -> None:
    lines = ["# RAG Evaluation Report", "",
             "> 指标语义（v1.10+）：faithfulness / context_precision 为 Ragas LLM judge；",
             "> answer_compliance 为自研 LLM judge 6 档制（0=未回答问题，1-5 合规度），",
             "> 0 分参与 compliance 均值（有答案但未回答 = 生成质量缺陷）；",
             "> unanswered_rate 为系统未作答样本（refused/timeout/空答案）占比；",
             "> style_consistency 为 vs 风格规范绝对打分（全量答案）；",
             "> refusal_appropriateness 为纯规则四场景判定。",
             "",
             "| Config | Faithfulness | Context Precision | Answer Compliance | "
             "Refusal Appropriateness | Style Consistency | P50 (ms) | P95 (ms) | Avg Tokens | Requests |",
             "|--------|-------------|-------------------|-------------------|----------------------|"
             "------------------|----------|----------|------------|----------|"]
    for r in results:
        cfg = r.get("config_name", "")
        cells = [cfg]
        for col in ["faithfulness", "context_precision", "answer_compliance",
                    "refusal_appropriateness", "style_consistency",
                    "p50_latency_ms", "p95_latency_ms", "avg_tokens_per_call",
                    "total_requests"]:
            val = r.get(col)
            cells.append("—" if val is None else f"{val:g}" if isinstance(val, float) else str(val))
        lines.append("| " + " | ".join(cells) + " |")

    # P4 分层视图：OOS 子集 / 正常业务子集 / 未作答三类分解
    # （OOS 样本不进 CP/Faith 计算但混在 refusal/unanswered 里，拆分后才能
    # 区分"OOS 防御能力"与"正常业务误拒"；Style 与检索配置正交的特性
    # 也在此注明：style 用于验证 Prompt 约束，不用于检索方案选型）
    lines.append("")
    lines.append("## 分层视图")
    lines.append("")
    lines.append("| Config | OOS 拒答率（OOS 子集） | 正常业务拒答正确率（误拒检测） | 未作答分解 refused/timeout/空 |")
    lines.append("|--------|------|------|------|")
    for r in results:
        cfg = r.get("config_name", "")
        oos = r.get("oos_refusal_rate")
        normal = r.get("normal_refusal_rate")
        oos_s = "—" if oos is None else f"{oos:g}"
        normal_s = "—" if normal is None else f"{normal:g}"
        parts = [str(r.get("unanswered_refused", 0)),
                 str(r.get("unanswered_timeout", 0)),
                 str(r.get("unanswered_empty", 0))]
        lines.append(f"| {cfg} | {oos_s} | {normal_s} | {'/'.join(parts)} |")
    lines.append("")
    lines.append("> 注：OOS 拒答率 = OOS 样本中正确拒答占比（测 OOS 防御能力）；")
    lines.append("> 正常业务拒答正确率 = 非 OOS 样本中未误拒占比（测误拒）；")
    lines.append("> 未作答三类：refused（拒答）/ timeout（阶段超时降级）/ 空（其余空答案）。")
    lines.append("> Style 指标与检索配置正交（三配置近同），用于验证 Prompt 话术约束而非检索方案选型。")

    lines.append("")
    lines.append("## 结论")
    best_lines = []
    for col in _COMPARED_METRICS:
        best = _best_config(results, col)
        if best:
            cfg, val = best
            direction = "最高" if _METRIC_DIRECTION[col] == "higher" else "最低"
            best_lines.append("- {label} 最优：**{cfg}**（{val:g}，{direction}）".format(
                label=_METRIC_LABEL[col], cfg=cfg, val=val, direction=direction))
    if best_lines:
        lines.extend(best_lines)
    else:
        lines.append("- 所有指标均无可用数据（Ragas 不可用且无规则指标？请检查评估日志）。")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _best_config(results: List[dict], metric: str):
    """返回 (config_name, value) 指标最优配置；无数据 → None。"""
    candidates = [(r.get("config_name"), r.get(metric)) for r in results
                  if r.get(metric) is not None]
    if not candidates:
        return None
    higher = _METRIC_DIRECTION.get(metric, "higher") == "higher"
    return max(candidates, key=lambda kv: kv[1]) if higher \
        else min(candidates, key=lambda kv: kv[1])


# ── before/after 对比 ──────────────────────────────────────

def compare_runs(run_id_before: str, run_id_after: str) -> dict:
    """按 config_name 对齐两次运行，计算每指标 delta_pct。

    返回 {config_name: {metric: {"before", "after", "delta_pct"}}, ...}。
    测试集 hash 不一致 → ValueError("test set mismatch")。
    """
    rows_before = asyncio.run(_fetch_history(run_id_before))
    rows_after = asyncio.run(_fetch_history(run_id_after))
    if not rows_before:
        raise ValueError(f"eval_history 无 run_id={run_id_before!r} 的记录")
    if not rows_after:
        raise ValueError(f"eval_history 无 run_id={run_id_after!r} 的记录")

    hash_before = {r["config_name"]: r["test_set_hash"] for r in rows_before}
    hash_after = {r["config_name"]: r["test_set_hash"] for r in rows_after}
    for cfg in sorted(set(hash_before) & set(hash_after)):
        if hash_before[cfg] != hash_after[cfg]:
            raise ValueError("test set mismatch")

    by_cfg_before = {r["config_name"]: r for r in rows_before}
    by_cfg_after = {r["config_name"]: r for r in rows_after}
    result = {}
    for cfg in sorted(set(by_cfg_before) & set(by_cfg_after)):
        rb, ra = by_cfg_before[cfg], by_cfg_after[cfg]
        result[cfg] = {}
        for col in _COMPARED_METRICS:
            before, after = rb[col], ra[col]
            delta = None
            if before is not None and after is not None and before != 0:
                delta = round((after - before) / before * 100, 2)
            result[cfg][col] = {"before": before, "after": after, "delta_pct": delta}
    return result


async def _fetch_history(run_id: str) -> List[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT run_id, config_name, test_set_hash, total_qa_pairs, "
            "faithfulness, context_precision, context_recall, answer_relevancy, "
            "answer_compliance, style_consistency, refusal_appropriateness, "
            "p50_latency_ms, p95_latency_ms, avg_tokens_per_call, "
            "total_pii_redactions, total_injections_blocked "
            "FROM eval_history WHERE run_id = ? ORDER BY config_name", (run_id,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()
