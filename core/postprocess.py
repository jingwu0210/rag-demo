"""生成后处理：PII 脱敏（PIIScrubber）+ 拒答检查（RefusalCheck）+ 编排（PostProcessor）

- PIIScrubber: 正则从 config pii.patterns 动态加载，无 config 时用内置默认
- RefusalCheck: 三规则（低置信度 / 超出知识库范围 / 安全敏感），retrieval_result
  兼容 RetrievalResult 与任何有 .docs 属性的对象
- PostProcessor: 先拒答检查，拒答返回模板话术；否则脱敏后返回
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from core.config import ConfigRegistry


@dataclass
class PostProcessResult:
    answer: str
    refused: bool = False
    refusal_reason: Optional[str] = None     # "low_confidence" | "out_of_scope" | "safety"
    pii_redact_count: int = 0


class PIIScrubber:
    """PII 脱敏器：config pii.patterns（name/regex/replacement）→ re.subn 计数"""

    # 内置默认（config pii.patterns 缺失时兜底），与设计文档 §4.6 一致
    DEFAULT_PATTERNS: List[Tuple[str, str, str]] = [
        ("china_id", r"[1-9]\d{5}(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx]",
         "***[REDACTED_ID]***"),
        ("mobile", r"1[3-9]\d{9}",
         "***[REDACTED_PHONE]***"),
        ("email", r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
         "***[REDACTED_EMAIL]***"),
        ("ip_address", r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}",
         "***[REDACTED_IP]***"),
    ]

    def __init__(self):
        patterns_cfg = ConfigRegistry.get("pii.patterns")
        if patterns_cfg:
            self.PATTERNS = [
                (p["name"], re.compile(p["regex"]), p["replacement"])
                for p in patterns_cfg
            ]
        else:
            self.PATTERNS = [
                (name, re.compile(pattern), replacement)
                for name, pattern, replacement in self.DEFAULT_PATTERNS
            ]

    def redact(self, text: str) -> Tuple[str, int]:
        """脱敏 + 返回触发次数（用于写入 request_metrics.pii_redact_count）"""
        count = 0
        for _name, pattern, replacement in self.PATTERNS:
            text, n = re.subn(pattern, replacement, text)
            count += n
        return text, count


# 拒答话术内置兜底（config refusal.responses.{reason} 取不到时使用，英文文本）
_FALLBACK_RESPONSES = {
    "low_confidence": "Sorry, I could not find sufficiently relevant information "
                      "in the internal knowledge base. Please try rephrasing your "
                      "question or contact the relevant department.",
    "out_of_scope": "Your question is outside the coverage of the internal "
                    "knowledge base. I can only answer questions related to "
                    "company internal documents.",
    "safety": "I am unable to answer questions involving security-sensitive content.",
}


class RefusalCheck:
    """拒答检查：低置信度 / 超出知识库范围 / 安全敏感 三规则"""

    def __init__(self, confidence_threshold: Optional[float] = None):
        if confidence_threshold is None:
            confidence_threshold = ConfigRegistry.get(
                "refusal.confidence_threshold", 0.45)
        self.confidence_threshold = float(confidence_threshold)

    def evaluate(self, query: str, retrieval_result, mode: str = None) -> Tuple[bool, Optional[str]]:
        """返回 (是否拒答, 拒答原因)；retrieval_result 需有 .docs 属性

        OOS 判定两层（v1.6：删除关键词层）：
        ① 空结果 → 拒答（所有模式）
        ② 置信度信号 → 拒答：
           - vector-only: docs[0].score 余弦（0-1）与 confidence_threshold 比较
           - hybrid / hybrid+rerank: 用 vector_top1_sim 旁路信号（余弦 0-1 有绝对语义）。
             RRF 分数是语料内相对排名，OOS 问题的 top1 RRF 照样高分 → 拦不住 OOS
             （评估实测 hybrid OOS 漏拒 8/10 的根因）；CrossEncoder 分数无界不可比。
             旁路信号缺失（旧结构/mock）时回退到"仅空结果拒答"。
        ③ sensitive 关键词 → safety（保留：危险内容拦截不依赖"是否相关"）

        v1.6 变更：out_of_scope 关键词层删除 — 静态黑名单表达"什么不在知识库"
        覆盖不全（"挖矿"漏网）且误伤（"股票期权激励政策"被"股票"拦截），
        置信度信号已覆盖其正确场景（明显越界问题 top1_sim 必然低）。
        扩展点：日后如需零成本快速拦截明显越界，可在此重新引入启发式层。
        """
        docs = getattr(retrieval_result, "docs", None) or []
        mode = mode or ConfigRegistry.get("retrieval.mode", "hybrid+rerank")

        # 规则 1: 空结果 → low_confidence（所有模式）
        if not docs:
            return True, "low_confidence"

        # 规则 2: 置信度不足 → low_confidence（按模式选择信号）
        if mode == "vector-only":
            if docs[0].score < self.confidence_threshold:
                return True, "low_confidence"
        else:
            # hybrid 系：用向量路 top1 余弦旁路信号（RetrievalResult.vector_top1_sim）
            top1_sim = getattr(retrieval_result, "vector_top1_sim", None)
            if top1_sim is not None and top1_sim < self.confidence_threshold:
                return True, "low_confidence"

        # 规则 3: query 命中 sensitive 关键词 → safety（保留）
        for kw in ConfigRegistry.get("refusal.rules.sensitive_keywords", []) or []:
            if kw in query:
                return True, "safety"

        return False, None


class PostProcessor:
    """后处理编排：RefusalCheck → 拒答话术 / PIIScrubber 脱敏"""

    def __init__(self, refusal_check: Optional[RefusalCheck] = None):
        self.refusal_check = refusal_check or RefusalCheck()
        self._scrubber = PIIScrubber()

    def process(self, answer: str, query: str, retrieval_result, mode: str = None) -> PostProcessResult:
        refused, reason = self.refusal_check.evaluate(query, retrieval_result, mode=mode)
        if refused:
            template = ConfigRegistry.get(
                f"refusal.responses.{reason}",
                _FALLBACK_RESPONSES.get(reason, ""),
            )
            return PostProcessResult(
                answer=template, refused=True, refusal_reason=reason)
        redacted, count = self._scrubber.redact(answer)
        return PostProcessResult(answer=redacted, pii_redact_count=count)
