"""对话压缩（ConversationCompressor）

超过 conversation.max_history_turns（默认 3）轮时：
保留最近 N 轮原文（recent_turns）+ 更早轮次 LLM 摘要（一次轻量调用）。
摘要失败降级为 summary=None（compressed=False），不抛异常，近轮原文始终保留。
generator 可注入 mock；None 时惰性构建（只 import 不执行直到调用，避免循环依赖）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from core.config import ConfigRegistry
from core.logging_config import get_logger

logger = get_logger(module=__name__)

# 摘要指令（作为 PromptContext.question 传给 LLM）
SUMMARY_QUESTION = "请将以下对话压缩为不超过100字的中文摘要"


@dataclass
class CompressedHistory:
    recent_turns: list = field(default_factory=list)   # 最近 N 轮 [{"query","answer"}]
    summary: Optional[str] = None
    compressed: bool = False


class ConversationCompressor:
    """轻量压缩：近轮原文 + 早期轮次摘要；LLM 失败时安全降级"""

    def __init__(self, generator=None):
        self.generator = generator
        self.max_history_turns = int(
            ConfigRegistry.get("conversation.max_history_turns", 3))

    async def compress(self, turns: list) -> CompressedHistory:
        """压缩 turns（[{"query","answer"}, ...]）：
        <= max_history_turns → 原文直返不压缩；否则最近 N 轮原文 + 早期摘要"""
        if len(turns) <= self.max_history_turns:
            return CompressedHistory(
                recent_turns=list(turns), summary=None, compressed=False)

        recent = list(turns[-self.max_history_turns:])
        older = list(turns[:-self.max_history_turns])
        try:
            summary = await self._summarize(older)
        except Exception:
            logger.warning(
                "compress_summary_failed",
                older_turns=len(older),
                max_history_turns=self.max_history_turns,
            )
            return CompressedHistory(
                recent_turns=recent, summary=None, compressed=False)
        return CompressedHistory(
            recent_turns=recent, summary=summary, compressed=True)

    async def _summarize(self, older: list) -> str:
        # 惰性 import：避免模块级循环依赖（generator → prompt → retriever）
        if self.generator is None:
            from core.generator import Generator
            self.generator = Generator()
        from core.prompt import PromptContext

        ctx = PromptContext(
            question=SUMMARY_QUESTION,
            documents=[],
            history=older,          # 早期轮次 [{"query","answer"}, ...] 原样传入
        )
        result = await self.generator.generate(ctx)
        return result.text.strip()
