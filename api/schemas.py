"""API Layer — Pydantic Schemas（请求/响应契约）

与 services.chat.ChatResponse（dataclass）字段一一对应；
/ingest 与 core.versioned.IngestResult 字段一一对应。
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ChatRequest(BaseModel):
    query: str
    session_id: Optional[str] = None
    # 请求级检索模式（vector-only | hybrid | hybrid+rerank）；None = 用 config 全局值
    mode: Optional[str] = None


class ChatResponseSchema(BaseModel):
    answer: str
    session_id: str = ""
    # 实际使用的检索模式（请求级覆盖生效后的值；超时降级路径用请求生效值）
    mode: str = ""
    sources: List[Dict[str, Any]] = []
    timing_ms: Dict[str, int] = {}
    token_usage: Dict[str, int] = {}
    refused: bool = False
    refusal_reason: Optional[str] = None
    from_cache: bool = False
    partial: bool = False


class IngestResponse(BaseModel):
    status: str                # ingested | skipped | replaced
    chunks_created: int = 0
    chunks_replaced: int = 0
    doc_hash: str = ""
    source_file: str = ""
    version: str = ""
    reason: str = ""


class HealthResponse(BaseModel):
    status: str                # ok | degraded
    components: Dict[str, str]
    concurrency: Dict[str, int]    # {"active": n, "max": 10}
