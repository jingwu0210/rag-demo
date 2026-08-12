"""安全防护 Layer 1：InjectionScanner — 检索后、Prompt 构建前逐 chunk 扫描注入模式"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from core.logging_config import get_logger
from core.retriever import ScoredDoc


class InjectionScanner:
    """注入扫描器：命中 severity >= threshold 的模式 → block（移出列表）；低于 → warn 保留

    模式库见设计文档 §6.1。默认 severity_threshold="medium"，当前模式库全为 high，
    因此默认只 block high；low/medium 命中走 warn 分支（扩展位）。
    """

    PATTERNS = [
        # 指令覆盖（{1,2} 扩展：兼容 "ignore all instructions" 与 "ignore all previous instructions"）
        (r"(?i)(ignore|disregard|override|forget)\s+(?:(?:all|previous|above)\s+){1,2}(instructions?|rules?|prompts?)", "high"),
        # 角色劫持
        (r"(?i)(you\s+are\s+now|from\s+now\s+on\s+you|your\s+new\s+(role|task|identity))", "high"),
        # 数据外泄（对话历史）
        (r"(?i)(output|print|display|show)\s+(the\s+)?(conversation|chat)\s+(history|log)", "high"),
        # 数据外泄（外发）
        (r"(?i)(send|post|upload)\s+(this|the)\s+(data|conversation|output)\s+to", "high"),
    ]
    _SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}

    def __init__(self, severity_threshold: str = "medium"):
        self.severity_threshold = severity_threshold
        self.logger = get_logger(module="scanner")

    def scan(self, chunks: List[ScoredDoc]) -> Tuple[List[ScoredDoc], int]:
        """对每个 chunk 跑全部正则；返回 (cleaned_chunks, blocked_count)"""
        cleaned: List[ScoredDoc] = []
        blocked = 0
        for chunk in chunks:
            hit = self._first_hit(chunk.text)
            if hit is None:
                cleaned.append(chunk)
                continue
            pattern, severity = hit
            if self._severity_gte(severity, self.severity_threshold):
                self.logger.warning("injection_detected", chunk_id=chunk.chunk_id,
                                    pattern=pattern, severity=severity, action="block")
                blocked += 1
            else:
                # 扩展位：低于阈值的可疑内容不 block，保留但记录告警
                self.logger.info("injection_warn", chunk_id=chunk.chunk_id,
                                 pattern=pattern, severity=severity, action="warn")
                cleaned.append(chunk)
        return cleaned, blocked

    def _first_hit(self, text: str) -> Optional[Tuple[str, str]]:
        for pattern, severity in self.PATTERNS:
            if re.search(pattern, text):
                return pattern, severity
        return None

    @classmethod
    def _severity_gte(cls, severity: str, threshold: str) -> bool:
        return cls._SEVERITY_RANK.get(severity, -1) >= cls._SEVERITY_RANK.get(threshold, 0)
