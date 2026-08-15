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
import random
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


def _warmup_reranker(chat_service) -> None:
    """B 方案（v1.8）：QA 计时前预热 reranker 模型（首次加载 ~5.5s）。

    互斥锁（R8）保证并发首触只加载一次；预热失败记 warning 不中断评估 —
    运行时每个请求的 rerank 前仍有 ensure_loaded + 降级兜底。
    """
    try:
        chat_service.retrieval.reranker.ensure_loaded()
        logger.info("reranker_warmup_complete")
    except Exception:
        logger.warning("reranker_warmup_failed", exc_info=True)


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
                        ground_truth: str, is_out_of_scope: bool = False):
    """构造 Ragas SingleTurnSample；不可构造 → None（记日志，不中断评估）。

    R4: OOS 问题直接返回 None — ground_truth 为空 → reference=None 会让
    ContextPrecision 抛 KeyError('reference')（评估实测 10 次崩溃）；拒答回答
    无断言可判，Faithfulness 对 OOS 也无意义。
    """
    if is_out_of_scope:
        return None
    if SingleTurnSample is None:
        logger.info("ragas_skipped", reason="ragas 不可导入")
        return None
    try:
        return SingleTurnSample(
            user_input=question,
            response=answer,
            # I-4 终审：优先用 chunk 真实文本喂 judge（chat 组装 sources 时已带 text）；
            # 旧结构（无 text 的 mock/历史数据）回退 heading_path
            retrieved_contexts=[s.get("text") or s.get("heading_path", "")
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


async def _try_ragas_score_async(sample, metric, metric_name: str) -> Optional[float]:
    """Ragas 异步版（并发评估用）：在现有事件循环内 await，避免嵌套 asyncio.run。"""
    try:
        score = await metric.single_turn_ascore(sample)
        return float(score)
    except Exception as exc:
        logger.warning("ragas_metric_failed", metric=metric_name, error=str(exc))
        return None


# ── 自研 LLM judge（Answer Compliance / Style Consistency）──────────────────

COMPLIANCE_JUDGE_PROMPT = """你是合规打分裁判。给定【参考文档片段】和【模型回答】，按6档打分：
0分：回答未回答问题（如"文档中未包含/无法回答/无法给出确切答案"类表述），或答案与问题无关；
5分：完全依据文档，不添加、不遗漏、不修改任何规则/数字；
4分：仅极少量无关补充，关键信息完整准确；
3分：次要信息轻微遗漏，核心规则无错误；
2分：重要金额、流程、时效遗漏或篡改；
1分：大量编造内容，核心回答与文档冲突。
判定规则（必须遵守）：
1. 判定 0 分前，必须逐条核对参考文档；只有答案的关键断言在文档中完全找不到支撑时才能判 0。
2. 参考文档中含有无关内容时，不得因此扣分——只核对答案断言是否与文档中对应内容一致。
输出格式：分数|理由（理由一句话，≤30字）。

参考文档：
{chunks_text}

模型回答：
{llm_answer}"""

STYLE_RUBRIC_PROMPT = """你是写作风格裁判。对照以下企业知识库回答规范，给回答打 1-5 分：
规范：正式企业话术、结论先行、分点/结构化排版、引用来源标注、无口语化表达。
5=完全符合规范，4=基本符合，3=部分符合，2=明显偏离，1=完全不符合。
只输出数字分数。

回答：
{answer}"""


def _judge_llm_call(prompt: str, temperature: float = 0.0) -> Optional[tuple]:
    """单次 DeepSeek judge 调用 → (int 分数, str 理由)；任何失败 → None（不中断评估）。

    v1.9（A 方案）：解析"分数|理由"格式（理由持久化到 per_qa 供诊断）；
    纯数字输出（style prompt）→ 理由为 ""。
    """
    import httpx
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None
    base_url = ConfigRegistry.get("llm.base_url", "https://api.deepseek.com/v1")
    model = ConfigRegistry.get("eval.models.model", "deepseek-chat")
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": temperature, "max_tokens": 100})
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            # 提取第一个整数（兼容 judge 偶发多输出/前缀词如"分数：5分"）
            import re
            m = re.search(r"\d+", content)
            if m:
                score = int(m.group())
                score = min(max(score, 0), 5)   # v1.6: 0 分档（未回答问题）
                # 理由 = 最后一个 "|" 之后的内容（无 | 则整段或空）
                reason = content.split("|")[-1].strip() if "|" in content else ""
                return (score, reason)
            logger.warning("judge_unparsable", content=content[:100])
            return None
    except Exception as exc:
        logger.warning("judge_call_failed", error=str(exc))
        return None


async def _judge_batch_async(prompts: List[str], max_concurrency: int = 5) -> List[Optional[tuple]]:
    """批量 judge 调用（线程池并发 + 限流）；顺序与输入一致。"""
    sem = asyncio.Semaphore(max_concurrency)

    async def _one(prompt):
        async with sem:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, _judge_llm_call, prompt)

    return await asyncio.gather(*[_one(p) for p in prompts])


def _judge_compliance(answers_chunks: List[tuple]) -> List[Optional[tuple]]:
    """Answer Compliance LLM judge（6 档制，v1.9）。(answer, chunks_text) 批量打分。

    返回 [(score, reason)] — v1.9 起理由持久化进 per_qa（judge 误判可诊断，
    不必再重放 API）。
    """
    prompts = [COMPLIANCE_JUDGE_PROMPT.format(
        chunks_text=(chunks_text or "（无检索上下文）")[:4000],
        llm_answer=answer[:2000]) for answer, chunks_text in answers_chunks]
    if not prompts:
        return []
    return asyncio.run(_judge_batch_async(prompts))


def _judge_style_absolute(answers: List[str]) -> List[Optional[int]]:
    """Style 绝对打分（v1.4 重设计）：每个答案 vs 风格规范独立打 1-5 分。

    替代原 pairwise（配置内答案互比）：pairwise 测的是"答案集合内部格式方差"，
    且不同配置抽样对完全不同 → 跨配置不可比。绝对打分用同一规范同一 judge
    全量答案，跨配置可比且完全可复现（无抽样）。
    """
    prompts = [STYLE_RUBRIC_PROMPT.format(answer=a[:2000])
               for a in answers if a and a.strip()]
    if not prompts:
        return []
    pairs = asyncio.run(_judge_batch_async(prompts))
    return [p[0] if p is not None else None for p in pairs]


# ── 测试集 hash ─────────────────────────────────────────────

def compute_test_set_hash(test_set: List[dict]) -> str:
    """md5(json.dumps(sorted questions)) — 保证 before/after 对比基于同一测试集。"""
    questions = sorted(item["question"] for item in test_set)
    return hashlib.md5(json.dumps(questions, ensure_ascii=False).encode("utf-8")).hexdigest()


# ── 三配置对比 ──────────────────────────────────────────────

async def _evaluate_qa_async(chat_service, item: dict) -> dict:
    """单条 QA 的完整评估（并发单元）：问答 + Ragas judge（双指标并行）+ 拒答规则。

    返回 per_qa 条目（含所有指标与中间数据）。
    """
    question = item["question"]
    ground_truth = item.get("ground_truth", "") or ""
    is_out_of_scope = bool(item.get("is_out_of_scope", False))
    language = item.get("language", "")

    resp = await chat_service.process(question, None)
    answer = resp.answer or ""
    sources = list(resp.sources or [])
    latency = int((resp.timing_ms or {}).get("total", 0))
    usage = resp.token_usage or {}
    token_total = int(usage.get("total", 0))
    token_prompt = int(usage.get("prompt", 0))
    token_completion = int(usage.get("completion", 0))
    refused = bool(resp.refused)
    from_cache = bool(resp.from_cache)
    refusal_reason = resp.refusal_reason
    # R4: 超时检测 — 生成超时/部分返回 或 空回答且非拒答
    is_timeout = bool(resp.partial) or (
        not answer and not refused and not from_cache and not sources)

    # ── LLM judge 指标（Ragas；OOS/超时 → None 跳过；双指标并行）──
    sample = _build_ragas_sample(
        question, answer, sources, ground_truth,
        is_out_of_scope=is_out_of_scope or is_timeout)
    faithfulness = None
    context_precision = None
    if sample is not None and _ensure_ragas_llm():
        faithfulness, context_precision = await asyncio.gather(
            _try_ragas_score_async(sample, _ragas_faithfulness, "faithfulness"),
            _try_ragas_score_async(sample, _ragas_context_precision, "context_precision"),
        )

    # ── 拒答四场景（纯规则，含漏拒检测）──
    # 正确拒答（OOS 且拒）→ 1；正确作答（非 OOS 未拒且非漏拒）→ 1；
    # 误拒（非 OOS 拒）→ 0；漏拒（非 OOS 未拒但无 sources 且非缓存）→ 0；OOS 漏拒 → 0
    if is_out_of_scope:
        refusal_appropriateness = 1 if refused else 0
    elif refused:
        refusal_appropriateness = 0          # 误拒
    elif not sources and not from_cache and answer:
        refusal_appropriateness = 0          # 漏拒：无依据仍作答
    else:
        refusal_appropriateness = 1          # 正确作答

    chunks_text = ("\n\n---\n\n".join(
        (s.get("text") or s.get("heading_path", "")) for s in sources)
        if sources else "")

    return {
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
        "token_prompt": token_prompt,
        "token_completion": token_completion,
        "timeout": is_timeout,
        "faithfulness": faithfulness,
        "context_precision": context_precision,
        "refusal_appropriateness": refusal_appropriateness,
        "chunks_text": chunks_text,
    }


def run_comparison(chat_service, test_set: List[dict], run_id: str = None) -> List[dict]:
    """对 config eval.compare_configs 的三配置循环评估，聚合指标并写入 eval_history。

    并发策略（v1.7）：每配置内的问答并发执行（eval.concurrency，默认 5）；
    Ragas 双指标并行 gather；自研 judge 批量并发（已有）。
    返回 [{config_name, faithfulness, context_precision, answer_compliance,
           refusal_appropriateness, style_consistency, p50_latency_ms, p95_latency_ms,
           avg_tokens_per_call, total_requests, ...}] 列表（每 config 一条）。
    """
    run_id = run_id or "eval_{}_{}".format(
        time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:8])
    test_set_hash = compute_test_set_hash(test_set)
    modes = ConfigRegistry.get(
        "eval.compare_configs", ["vector-only", "hybrid", "hybrid+rerank"])
    concurrency = int(ConfigRegistry.get("eval.concurrency", 5))
    logger.info("eval_run_start", run_id=run_id, test_set_hash=test_set_hash,
                modes=modes, qa_count=len(test_set), concurrency=concurrency)

    results = []
    for mode in modes:
        apply_config_mode(mode)

        # B 方案（v1.8）：预热重模型 — reranker 首次加载 ~5.5s，挪到 QA 计时之前，
        # 65 条样本的延迟口径不含模型加载税（R4 的 50s P95 教训）。互斥锁（R8）
        # 保证并发首触只加载一次；预热失败不中断评估（运行时仍有 rerank 降级兜底）。
        if mode == "hybrid+rerank" and ConfigRegistry.get("reranker.enabled", True):
            _warmup_reranker(chat_service)

        # ── 并发执行全部 QA（Semaphore 限流）──
        _ensure_ragas_llm()  # 预配置 judge（一次性）
        sem = asyncio.Semaphore(concurrency)

        async def _one(item):
            async with sem:
                return await _evaluate_qa_async(chat_service, item)

        qa_results = asyncio.run(
            asyncio.gather(*[_one(i) for i in test_set]))

        # ── 聚合 ──
        per_qa: List[dict] = []
        latencies: List[int] = []
        tokens: List[int] = []
        prompt_tokens: List[int] = []
        completion_tokens: List[int] = []
        chunks_counts: List[int] = []
        faithfulness_scores: List[float] = []
        precision_scores: List[float] = []
        refusal_hits = 0
        timeout_count = 0
        total_pii = 0
        total_injections = 0
        compliance_items: List[tuple] = []

        for qa in qa_results:
            per_qa.append({k: v for k, v in qa.items() if k != "chunks_text"})
            latencies.append(qa["latency_ms"])
            tokens.append(qa["tokens_total"])
            prompt_tokens.append(qa["token_prompt"])
            completion_tokens.append(qa["token_completion"])
            chunks_counts.append(qa["sources_count"])
            if qa["faithfulness"] is not None:
                faithfulness_scores.append(qa["faithfulness"])
            if qa["context_precision"] is not None:
                precision_scores.append(qa["context_precision"])
            refusal_hits += qa["refusal_appropriateness"]
            if qa["timeout"]:
                timeout_count += 1
            if qa["answer"] and not qa["refused"] and not qa["timeout"]:
                compliance_items.append(
                    (qa["question"], qa["answer"], qa["chunks_text"]))

        # ── 自研 judge（每配置批量打分）──
        compliance_raw = _judge_compliance(
            [(answer, chunks_text) for _, answer, chunks_text in compliance_items])
        compliance_map = {}  # 按 question 回填 (score, reason)
        for (qa, pair) in zip(compliance_items, compliance_raw):
            compliance_map[qa[0]] = pair
        for q in per_qa:
            pair = compliance_map.get(q["question"])
            q["answer_compliance"] = pair[0] if pair is not None else None
            q["judge_reason"] = pair[1] if pair is not None else None
        # v1.10 口径变更：0 分（有答案但未回答问题）参与 compliance 均值 —
        # judge 已过滤 refused/timeout/空答案，"无法回答"式自由文本属于生成质量
        # 缺陷，应拉低 compliance；unanswered_rate 语义改为"系统未作答样本"
        # （refused / timeout / 空答案）占总样本比。
        unanswered_count = sum(
            1 for q in per_qa
            if q["refused"] or q["timeout"]
            or (not q["answer"] and not q["from_cache"]))
        unanswered_count_refused = sum(1 for q in per_qa if q["refused"])
        unanswered_count_timeout = timeout_count
        unanswered_count_empty = sum(
            1 for q in per_qa
            if not q["answer"] and not q["from_cache"]
            and not q["refused"] and not q["timeout"])
        compliance_scores = [p[0] for p in compliance_map.values()
                             if p is not None]

        # v1.4: Style 改为 vs 风格规范绝对打分（全量答案，跨配置可比）
        style_scores = _judge_style_absolute(
            [q["answer"] for q in per_qa if q["answer"] and not q["refused"]])
        valid_styles = [s for s in style_scores if s is not None]
        style = (round(sum(valid_styles) / len(valid_styles) / 5, 4)
                 if valid_styles else None)

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
            # Compliance: score/5 连续分取均值（4 分贡献 0.8）
            # v1.10: judge 0 分参与均值（有答案但未回答 = 生成质量缺陷）
            "answer_compliance": (_round_mean(compliance_scores) / 5
                                  if compliance_scores else None),
            # v1.10: 系统未作答（refused/timeout/空答案）占总样本比
            "unanswered_rate": round(unanswered_count / max(len(per_qa), 1), 4),
            "style_consistency": style,
            "refusal_appropriateness": round(refusal_hits / n, 4),
            "p50_latency_ms": int(np.percentile(latencies, 50)) if latencies else 0,
            "p95_latency_ms": int(np.percentile(latencies, 95)) if latencies else 0,
            "avg_tokens_per_call": int(np.mean(tokens)) if tokens else 0,
            # E3: token/chunk 分解观测（机制透明化）
            "avg_prompt_tokens": int(np.mean(prompt_tokens)) if prompt_tokens else 0,
            "avg_completion_tokens": int(np.mean(completion_tokens)) if completion_tokens else 0,
            "avg_chunks_per_call": round(float(np.mean(chunks_counts)), 2) if chunks_counts else 0,
            "timeout_rate": round(timeout_count / n, 4),
            "total_pii_redactions": total_pii,
            "total_injections_blocked": total_injections,
            # P4 分层视图：OOS/正常业务拆分 + 未作答三类分解
            # （OOS 样本不进 CP/Faith 计算，但混在 refusal/unanswered 里需拆分观测）
            "oos_refusal_rate": _round_mean(
                [q["refusal_appropriateness"] for q in per_qa if q["is_out_of_scope"]]),
            "normal_refusal_rate": _round_mean(
                [q["refusal_appropriateness"] for q in per_qa if not q["is_out_of_scope"]]),
            "unanswered_refused": unanswered_count_refused,
            "unanswered_timeout": unanswered_count_timeout,
            "unanswered_empty": unanswered_count_empty,
            "per_qa_results_json": json.dumps(per_qa, ensure_ascii=False),
        }
        asyncio.run(_save_eval_history(agg))
        results.append(agg)
        logger.info("eval_config_done", run_id=run_id, config=mode,
                    answer_compliance=agg["answer_compliance"],
                    refusal_appropriateness=agg["refusal_appropriateness"],
                    faithfulness=agg["faithfulness"],
                    style_consistency=agg["style_consistency"],
                    timeout_rate=agg["timeout_rate"])

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
            "total_pii_redactions, total_injections_blocked, timeout_rate, "
            "avg_prompt_tokens, avg_completion_tokens, avg_chunks_per_call, "
            "unanswered_rate, per_qa_results_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (agg["run_id"], agg["config_name"], agg["test_set_hash"], agg["total_requests"],
             agg["faithfulness"], agg["context_precision"], agg["context_recall"],
             agg["answer_relevancy"], agg["answer_compliance"], agg["style_consistency"],
             agg["refusal_appropriateness"], agg["p50_latency_ms"], agg["p95_latency_ms"],
             agg["avg_tokens_per_call"], agg["total_pii_redactions"],
             agg["total_injections_blocked"], agg["timeout_rate"],
             agg["avg_prompt_tokens"], agg["avg_completion_tokens"],
             agg["avg_chunks_per_call"], agg["unanswered_rate"],
             agg["per_qa_results_json"]))
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
    parser.add_argument("--output", default="workspace/results/",
                        help="报表输出目录（写入 eval_report.csv / eval_report.md），"
                             "默认 workspace/results/")
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
