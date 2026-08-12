"""Evaluation — 三配置对比 Runner

对 config eval.compare_configs 的三个检索配置（vector-only / hybrid / hybrid+rerank）循环：
apply_config_mode → 逐条 QA 调 chat_service.process → 计算指标 → 聚合 → 写入 eval_history。

指标策略（务实降级，Ragas 失败不中断评估）：
- faithfulness / context_precision：LLM judge（Ragas）。DEEPSEEK_API_KEY 存在且网络可达时
  算真实值（LLM 指向 DeepSeek OpenAI 兼容端点）；否则（无 key / 网络失败 / 调用异常）记 None
  并打日志说明跳过。
- answer_compliance：自建规则近似（无 LLM judge 时的降级定义）— answer 非空 且 未拒答 且
  （命中缓存 或 检索到 sources）→ 1，否则 0。报告需注明近似性。
- refusal_appropriateness：out_of_scope 且拒答 → 1；非 out_of_scope 且未拒答 → 1；否则 0。
- style_consistency：跳过（None）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from typing import List, Optional

import numpy as np

from core.config import ConfigRegistry
from core.logging_config import get_logger
from storage.sqlite_client import get_db

logger = get_logger(module="eval.runner")

try:  # ragas 导入失败也不中断评估（懒加载于 _ensure_ragas_llm 内，此处仅为类型引用）
    from ragas.dataset_schema import SingleTurnSample
    from ragas.metrics import context_precision as _ragas_context_precision
    from ragas.metrics import faithfulness as _ragas_faithfulness
    _RAGAS_IMPORTED = True
except ImportError:  # pragma: no cover
    SingleTurnSample = None  # type: ignore
    _ragas_faithfulness = None  # type: ignore
    _ragas_context_precision = None  # type: ignore
    _RAGAS_IMPORTED = False


# ── 配置模式 ────────────────────────────────────────────────

def apply_config_mode(mode: str) -> None:
    """把检索配置切换到目标模式。

    注意：mode 值（vector-only/hybrid/hybrid+rerank）与 core.retriever.Retriever 接受的
    取值完全一致 — Retriever.__init__ 不识别 "vector"，统一用 "vector-only"。
    """
    if mode == "vector-only":
        ConfigRegistry.override("retrieval.mode", "vector-only")
        ConfigRegistry.override("reranker.enabled", False)
    elif mode == "hybrid":
        ConfigRegistry.override("retrieval.mode", "hybrid")
        ConfigRegistry.override("reranker.enabled", False)
    elif mode == "hybrid+rerank":
        ConfigRegistry.override("retrieval.mode", "hybrid+rerank")
        ConfigRegistry.override("reranker.enabled", True)
    else:
        raise ValueError(f"unknown config mode: {mode}")


# ── Ragas（LLM judge）──────────────────────────────────────

_RAGAS_LLM_READY = False


def _ensure_ragas_llm() -> bool:
    """把 Ragas 的 LLM judge 指向 DeepSeek（OpenAI 兼容端点）。

    DEEPSEEK_API_KEY 缺失或配置/网络失败 → False（调用方记 None 跳过，不中断评估）。
    """
    global _RAGAS_LLM_READY
    if _RAGAS_LLM_READY:
        return True
    if not _RAGAS_IMPORTED:
        logger.info("ragas_skipped", reason="ragas 不可导入")
        return False
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        logger.info("ragas_skipped", reason="DEEPSEEK_API_KEY 未设置，无法使用 LLM judge")
        return False
    try:
        from langchain_openai import ChatOpenAI
        from ragas.llms.base import LangchainLLMWrapper

        base_url = ConfigRegistry.get("llm.base_url", "https://api.deepseek.com/v1")
        model = ConfigRegistry.get("eval.models.model", "deepseek-chat")
        llm = LangchainLLMWrapper(ChatOpenAI(model=model, api_key=api_key, base_url=base_url))
        _ragas_faithfulness.llm = llm
        _ragas_context_precision.llm = llm
        _RAGAS_LLM_READY = True
        logger.info("ragas_configured", model=model, base_url=base_url)
        return True
    except Exception as exc:
        logger.warning("ragas_configure_failed", error=str(exc))
        return False


def _build_ragas_sample(question: str, answer: str, sources: List[dict],
                        ground_truth: str):
    """构造 Ragas SingleTurnSample；Ragas 不可导入/构造失败 → None（记日志，不中断评估）。"""
    if SingleTurnSample is None:
        logger.info("ragas_skipped", reason="ragas 不可导入")
        return None
    try:
        return SingleTurnSample(
            user_input=question,
            response=answer,
            retrieved_contexts=[s.get("heading_path") or s.get("chunk_id", "")
                                for s in sources] or None,
            reference=ground_truth or None,
        )
    except Exception as exc:
        logger.warning("ragas_sample_failed", error=str(exc))
        return None


def _try_ragas_score(sample, metric, metric_name: str) -> Optional[float]:
    """尽力调用 Ragas single_turn_ascore；任何失败 → None（不中断评估）。"""
    try:
        if not _ensure_ragas_llm():
            return None
        score = asyncio.run(metric.single_turn_ascore(sample))
        return float(score)
    except Exception as exc:
        logger.warning("ragas_metric_failed", metric=metric_name, error=str(exc))
        return None


# ── 测试集 hash ─────────────────────────────────────────────

def compute_test_set_hash(test_set: List[dict]) -> str:
    """md5(json.dumps(sorted questions)) — 保证 before/after 对比基于同一测试集。"""
    questions = sorted(item["question"] for item in test_set)
    return hashlib.md5(json.dumps(questions, ensure_ascii=False).encode("utf-8")).hexdigest()


# ── 三配置对比 ──────────────────────────────────────────────

def run_comparison(chat_service, test_set: List[dict], run_id: str = None) -> List[dict]:
    """对 config eval.compare_configs 的三配置循环评估，聚合指标并写入 eval_history。

    返回 [{config_name, faithfulness, context_precision, answer_compliance,
           refusal_appropriateness, style_consistency, p50_latency_ms, p95_latency_ms,
           avg_tokens_per_call, total_requests, ...}] 列表（每 config 一条）。
    """
    run_id = run_id or "eval_{}_{}".format(
        time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:8])
    test_set_hash = compute_test_set_hash(test_set)
    modes = ConfigRegistry.get(
        "eval.compare_configs", ["vector-only", "hybrid", "hybrid+rerank"])
    logger.info("eval_run_start", run_id=run_id, test_set_hash=test_set_hash,
                modes=modes, qa_count=len(test_set))

    results = []
    for mode in modes:
        apply_config_mode(mode)
        per_qa: List[dict] = []
        latencies: List[int] = []
        tokens: List[int] = []
        faithfulness_scores: List[float] = []
        precision_scores: List[float] = []
        compliance_hits = 0
        refusal_hits = 0
        total_pii = 0
        total_injections = 0

        for item in test_set:
            question = item["question"]
            ground_truth = item.get("ground_truth", "") or ""
            is_out_of_scope = bool(item.get("is_out_of_scope", False))
            language = item.get("language", "")

            resp = asyncio.run(chat_service.process(question, None))
            answer = resp.answer or ""
            sources = list(resp.sources or [])
            latency = int((resp.timing_ms or {}).get("total", 0))
            token_total = int((resp.token_usage or {}).get("total", 0))
            refused = bool(resp.refused)
            from_cache = bool(resp.from_cache)
            refusal_reason = resp.refusal_reason

            # ── LLM judge 指标（Ragas；不可用 → None）──
            sample = _build_ragas_sample(question, answer, sources, ground_truth)
            faithfulness = None
            context_precision = None
            if sample is not None:
                faithfulness = _try_ragas_score(
                    sample, _ragas_faithfulness, "faithfulness")
                context_precision = _try_ragas_score(
                    sample, _ragas_context_precision, "context_precision")

            # ── 规则近似指标（无 LLM judge 时的降级定义）──
            answer_compliance = 1 if (
                answer and not refused and (from_cache or len(sources) > 0)) else 0
            refusal_appropriateness = 1 if (
                (is_out_of_scope and refused) or (not is_out_of_scope and not refused)) else 0

            per_qa.append({
                "question": question,
                "language": language,
                "is_out_of_scope": is_out_of_scope,
                "answer": answer,
                "refused": refused,
                "refusal_reason": refusal_reason,
                "from_cache": from_cache,
                "sources_count": len(sources),
                "latency_ms": latency,
                "tokens_total": token_total,
                "faithfulness": faithfulness,
                "context_precision": context_precision,
                "answer_compliance": answer_compliance,
                "refusal_appropriateness": refusal_appropriateness,
            })
            latencies.append(latency)
            tokens.append(token_total)
            if faithfulness is not None:
                faithfulness_scores.append(faithfulness)
            if context_precision is not None:
                precision_scores.append(context_precision)
            compliance_hits += answer_compliance
            refusal_hits += refusal_appropriateness

        n = max(len(test_set), 1)
        agg = {
            "config_name": mode,
            "run_id": run_id,
            "test_set_hash": test_set_hash,
            "total_requests": len(test_set),
            "faithfulness": _round_mean(faithfulness_scores),
            "context_precision": _round_mean(precision_scores),
            "context_recall": None,
            "answer_relevancy": None,
            "answer_compliance": round(compliance_hits / n, 4),
            "style_consistency": None,
            "refusal_appropriateness": round(refusal_hits / n, 4),
            "p50_latency_ms": int(np.percentile(latencies, 50)) if latencies else 0,
            "p95_latency_ms": int(np.percentile(latencies, 95)) if latencies else 0,
            "avg_tokens_per_call": int(np.mean(tokens)) if tokens else 0,
            "total_pii_redactions": total_pii,
            "total_injections_blocked": total_injections,
            "per_qa_results_json": json.dumps(per_qa, ensure_ascii=False),
        }
        asyncio.run(_save_eval_history(agg))
        results.append(agg)
        logger.info("eval_config_done", run_id=run_id, config=mode,
                    answer_compliance=agg["answer_compliance"],
                    refusal_appropriateness=agg["refusal_appropriateness"],
                    faithfulness=agg["faithfulness"])

    logger.info("eval_run_done", run_id=run_id, configs=len(results))
    return results


def _round_mean(scores: List[float]) -> Optional[float]:
    return round(float(np.mean(scores)), 4) if scores else None


async def _save_eval_history(agg: dict) -> None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO eval_history (run_id, config_name, test_set_hash, total_qa_pairs, "
            "faithfulness, context_precision, context_recall, answer_relevancy, "
            "answer_compliance, style_consistency, refusal_appropriateness, "
            "p50_latency_ms, p95_latency_ms, avg_tokens_per_call, "
            "total_pii_redactions, total_injections_blocked, per_qa_results_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (agg["run_id"], agg["config_name"], agg["test_set_hash"], agg["total_requests"],
             agg["faithfulness"], agg["context_precision"], agg["context_recall"],
             agg["answer_relevancy"], agg["answer_compliance"], agg["style_consistency"],
             agg["refusal_appropriateness"], agg["p50_latency_ms"], agg["p95_latency_ms"],
             agg["avg_tokens_per_call"], agg["total_pii_redactions"],
             agg["total_injections_blocked"], agg["per_qa_results_json"]))
        await db.commit()
    finally:
        await db.close()


if __name__ == "__main__":  # pragma: no cover
    """CLI 入口：python -m eval.runner --output <dir>

    加载测试集 → 构建服务栈（与 api/app.py startup 同构）→ run_comparison
    → generate_report（CSV + Markdown 写至 --output）。失败直接抛错退出。
    """
    import argparse

    from core.config import ConfigRegistry
    from core.logging_config import setup_logging
    from eval.report import generate_report
    from eval.test_set import load_test_set
    from storage.sqlite_client import init_db

    parser = argparse.ArgumentParser(description="RAG 三配置对比评估（CLI）")
    parser.add_argument("--output", default="data/eval/results/",
                        help="报表输出目录（写入 eval_report.csv / eval_report.md），"
                             "默认 data/eval/results/")
    parser.add_argument("--test-set", default=None,
                        help="测试集 JSON 路径，默认取 config eval.test_set_path")
    args = parser.parse_args()

    if ConfigRegistry._instance is None:
        ConfigRegistry.init("config.yaml")
    setup_logging()
    asyncio.run(init_db())

    from core.cache import CacheManager
    from core.compressor import ConversationCompressor
    from core.embedder import Embedder
    from core.generator import Generator
    from core.guard import ResilienceGuard
    from core.postprocess import PostProcessor
    from core.reranker import Reranker
    from core.retriever import Retriever
    from core.scanner import InjectionScanner
    from services.chat import ChatService
    from services.retrieval import RetrievalService
    from storage.chroma_client import ChromaStore

    chroma_store = ChromaStore()
    embedder = Embedder()          # BGE-M3 下载/加载失败 → 抛错（CLI 评估不降级）
    guard = ResilienceGuard()
    retriever = Retriever(chroma_store, embedder)
    retrieval_service = RetrievalService(
        retriever=retriever, reranker=Reranker(), scanner=InjectionScanner(),
        guard=guard)
    chat_service = ChatService(
        retrieval=retrieval_service, generator=Generator(),
        postprocessor=PostProcessor(), cache=CacheManager(),
        guard=guard, compressor=ConversationCompressor())

    test_set = load_test_set(args.test_set)
    results = run_comparison(chat_service, test_set)
    csv_path = generate_report(results, args.output)
    print(f"评估完成: {csv_path}")
