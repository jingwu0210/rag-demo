"""LLM Provider 适配层（Adapter 模式）+ Generator 编排

- BaseLLMAdapter: 所有 Provider 的统一异步接口
- DeepSeekAdapter / QwenAdapter / GLMAdapter: OpenAI 兼容协议实现（同构，config 驱动）
- Generator: PromptBuilder.build → adapter.chat → 补 latency_ms
"""
from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx

from core.config import ConfigRegistry
from core.prompt import PromptBuilder, PromptContext


@dataclass
class GenerationResult:
    text: str
    token_prompt: int = 0
    token_completion: int = 0
    token_total: int = 0
    latency_ms: int = 0


class BaseLLMAdapter(ABC):
    """所有 LLM Provider 的统一接口"""

    @abstractmethod
    async def chat(self, messages: List[dict]) -> GenerationResult:
        ...


class _OpenAICompatAdapter(BaseLLMAdapter):
    """OpenAI 兼容协议实现基类（DeepSeek/Qwen/GLM 当前均走该协议，后续按各自协议调整）"""

    def __init__(self, api_key: Optional[str] = None):
        api_key_env = ConfigRegistry.get("llm.api_key_env", "DEEPSEEK_API_KEY")
        # api_key 显式传入优先；否则从环境变量读取，不硬编码密钥
        self.api_key = api_key or os.environ.get(api_key_env, "")
        self.base_url = ConfigRegistry.get("llm.base_url", "https://api.deepseek.com/v1")
        self.model = ConfigRegistry.get("llm.model", "deepseek-v4-flash")
        self.temperature = ConfigRegistry.get("llm.temperature", 0.1)
        self.max_tokens = ConfigRegistry.get("llm.max_tokens", 1024)
        self.timeout = ConfigRegistry.get("llm.timeout", 5)

    async def chat(self, messages: List[dict]) -> GenerationResult:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                },
            )
            # 非 2xx：带前 200 字符错误信息抛 RuntimeError
            status = resp.status_code
            if isinstance(status, int) and status >= 400:
                raise RuntimeError(
                    f"LLM API error {status}: {resp.text[:200]}"
                )
            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {}) or {}
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise RuntimeError(
                    f"LLM API 响应解析失败: {str(e)[:200]}"
                ) from e
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
            return GenerationResult(
                text=content,
                token_prompt=prompt_tokens,
                token_completion=completion_tokens,
                token_total=prompt_tokens + completion_tokens,
            )


class DeepSeekAdapter(_OpenAICompatAdapter):
    """DeepSeek 官方 OpenAI 兼容接口"""


class QwenAdapter(_OpenAICompatAdapter):
    """通义千问 DashScope 兼容接口（结构同 DeepSeek，后续按 DashScope 协议调整）"""


class GLMAdapter(_OpenAICompatAdapter):
    """智谱 GLM 兼容接口（结构同 DeepSeek，后续按协议调整）"""


_ADAPTER_FACTORY: Dict[str, type] = {
    "deepseek": DeepSeekAdapter,
    "qwen": QwenAdapter,
    "glm": GLMAdapter,
}


class Generator:
    """生成编排：PromptBuilder → Adapter → 补 latency_ms"""

    def __init__(self, adapter: Optional[BaseLLMAdapter] = None):
        if adapter is None:
            provider = ConfigRegistry.get("llm.provider", "deepseek")
            adapter_cls = _ADAPTER_FACTORY.get(provider)
            if adapter_cls is None:
                raise ValueError(f"unknown llm.provider: {provider}")
            adapter = adapter_cls()
        self.adapter = adapter

    async def generate(self, ctx: PromptContext) -> GenerationResult:
        messages = PromptBuilder.build(ctx)
        start = time.perf_counter()
        result = await self.adapter.chat(messages)
        result.latency_ms = int((time.perf_counter() - start) * 1000)
        return result
