"""Replay R7 非 5 分 compliance 样本，验证「恒 5 / 空返回」根因。

背景：deepseek-v4-flash 是推理模型，返回体含 reasoning_content（思考）与
content（最终答案）。_judge_llm_call 的 max_tokens=100 太短，思考吃光 token
导致 content 为空 → judge_unparsable → None。本脚本直接调 API，max_tokens 可配，
观察给足 token 后：(1) content 是否非空；(2) 分数是否有区分度。

只读诊断：不改代码、不写库。

用法：
    .venv/bin/python scripts/replay_compliance_judge.py [--max-tokens 2000] [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from collections import Counter

import httpx

from core.config import ConfigRegistry
from core.logging_config import setup_logging

RUN_ID = "eval_20260815_173117_b48364fd"  # R7（deepseek-chat 判别分产物）

PROMPT = """你是合规打分裁判。给定【参考文档片段】和【模型回答】，按6档打分：
0分：回答未回答问题（如"文档中未包含/无法回答/无法给出确切答案"类表述），或答案与问题无关；
5分：完全依据文档，不添加、不遗漏、不修改任何规则/数字；
4分：仅极少量无关补充，关键信息完整准确；
3分：次要信息轻微遗漏，核心规则无错误；
2分：重要金额、流程、时效遗漏或篡改；
1分：大量编造内容，核心回答与文档冲突。
判定规则（必须遵守）：
1. 判定 0 分前，必须逐条核对参考文档；只有答案的关键断言在文档中完全找不到支撑时才能判 0。
2. 参考文档中含有无关内容时，不得因此扣分——只核对答案断言是否与文档中对应内容一致。
3. 参考文档仅为检索片段，可能未包含语料全部条款。答案引用某条款编号而该条款不在参考文档中时，不得判为"编造"，应标注"无法核对"并按答案与文档的一致性给分。
4. 涉及个人信息（姓名/电话/身份证/银行卡号）的问题，答案使用 [REDACTED] 占位符脱敏或明确拒答的，视为完全正确（5 分）；不得以"关键信息缺失"为由扣分。
输出格式：分数|理由（理由一句话，≤30字）。

参考文档：
{chunks_text}

模型回答：
{llm_answer}"""


def load_samples(run_id: str, limit: int | None) -> list[tuple]:
    conn = sqlite3.connect("workspace/cache.db")
    c = conn.cursor()
    c.execute(
        "SELECT config_name, per_qa_results_json FROM eval_history WHERE run_id=?",
        (run_id,))
    samples = []
    for cfg, j in c.fetchall():
        for q in json.loads(j):
            score = q.get("answer_compliance")
            if score is not None and score < 5:
                samples.append((cfg, q["question"], q["answer"] or "",
                                score, q.get("judge_reason") or ""))
    conn.close()
    samples.sort(key=lambda s: s[0])
    return samples[:limit] if limit else samples


def build_retrieval_service():
    from core.embedder import Embedder
    from core.guard import ResilienceGuard
    from core.reranker import Reranker
    from core.retriever import Retriever
    from core.scanner import InjectionScanner
    from services.retrieval import RetrievalService
    from storage.chroma_client import ChromaStore

    return RetrievalService(
        retriever=Retriever(ChromaStore(), Embedder()), reranker=Reranker(),
        scanner=InjectionScanner(), guard=ResilienceGuard())


def judge_call(prompt: str, max_tokens: int) -> dict:
    """直接调 DeepSeek，返回 {content, reasoning_len, finish_reason, score}。"""
    key = os.getenv("DEEPSEEK_API_KEY")
    base = ConfigRegistry.get("llm.base_url", "https://api.deepseek.com/v1")
    model = ConfigRegistry.get("eval.models.model", "deepseek-v4-flash")
    with httpx.Client(timeout=120) as c:
        r = c.post(f"{base}/chat/completions",
                   headers={"Authorization": f"Bearer {key}"},
                   json={"model": model,
                         "messages": [{"role": "user", "content": prompt}],
                         "temperature": 0.0, "max_tokens": max_tokens})
        j = r.json()
    ch = j.get("choices", [{}])[0]
    msg = ch.get("message", {})
    content = (msg.get("content") or "").strip()
    reasoning = msg.get("reasoning_content") or ""
    score = None
    if content:
        import re
        m = re.search(r"\d+", content)
        if m:
            score = min(max(int(m.group()), 0), 5)
    return {"content": content, "reasoning_len": len(reasoning),
            "finish_reason": ch.get("finish_reason"), "score": score}


async def replay(samples, max_tokens: int) -> list[dict]:
    from eval.runner import apply_config_mode

    rs = build_retrieval_service()
    out = []
    for cfg, question, answer, orig_score, orig_reason in samples:
        apply_config_mode(cfg)
        ro = await rs.retrieve(question, None, cfg)
        docs = ro.docs if ro else []
        chunks = ("\n\n---\n\n".join(
            (d.text or (d.metadata or {}).get("heading_path", "")) for d in docs)
            if docs else "")
        prompt = PROMPT.format(chunks_text=chunks[:4000],
                               llm_answer=answer[:2000])
        r = judge_call(prompt, max_tokens)
        out.append({"config": cfg, "question": question, "answer": answer,
                    "orig": orig_score, "orig_reason": orig_reason, **r})
        print(f"[{cfg}] {orig_score}→{r['score']} "
              f"(reasoning={r['reasoning_len']}tok, finish={r['finish_reason']}) "
              f"{question[:40]}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=RUN_ID)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--max-tokens", type=int, default=2000)
    args = ap.parse_args()

    if not os.getenv("DEEPSEEK_API_KEY"):
        print("❌ 缺少 DEEPSEEK_API_KEY", flush=True)
        return 1
    if ConfigRegistry._instance is None:
        ConfigRegistry.init("config.yaml")
    setup_logging()

    samples = load_samples(args.run_id, args.limit)
    print(f"共 {len(samples)} 条非 5 分样本，judge = "
          f"{ConfigRegistry.get('eval.models.model')}，max_tokens={args.max_tokens}",
          flush=True)

    results = asyncio.run(replay(samples, args.max_tokens))

    scored = [r for r in results if r["score"] is not None]
    empty = [r for r in results if r["score"] is None]
    flipped = [r for r in scored if r["score"] == 5 and r["orig"] < 5]
    kept = [r for r in scored if r["score"] < 5]
    print("\n" + "=" * 70)
    print(f"空返回（None）: {len(empty)}/{len(results)}")
    print(f"有分样本 orig→new 分布: "
          f"{dict(sorted(Counter((r['orig'], r['score']) for r in scored).items()))}")
    print(f"翻成 5 分: {len(flipped)}，仍判低分: {len(kept)}")
    print("\n--- 仍判低分（有区分度）样本 ---")
    for r in kept:
        print(f"[{r['orig']}→{r['score']}] {r['config']}  {r['question'][:46]}")
        print(f"    new: {r['content'][:80]}")
    print("\n--- 翻成 5 分样本（reasoning 长度) ---")
    for r in flipped:
        print(f"[{r['orig']}→5] {r['config']}  {r['question'][:46]} "
              f"(reasoning={r['reasoning_len']}tok)")
        print(f"    new: {r['content'][:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
