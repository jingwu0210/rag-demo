"""API Layer — 应用入口

startup 构建全局单例存入 app.state（无模块级全局）：
ChromaStore/Embedder/Retriever/Reranker/Scanner/Generator/PostProcessor/
CacheManager/ResilienceGuard/ConversationCompressor → RetrievalService/ChatService/IngestService

- Embedder（BGE-M3）等在 startup 加载较重：整体 try/except，失败不崩，
  记 error 日志 + app.state.startup_error，health 显示 degraded
- 共享同一个 ResilienceGuard 实例：API 层限流信号量与 ChatService 阶段超时同源
"""
from __future__ import annotations

from fastapi import FastAPI

from core.config import ConfigRegistry
from core.logging_config import get_logger, setup_logging
from storage.sqlite_client import init_db

from api.routes import router

logger = get_logger(module="api.app")

app = FastAPI(title="RAG QA Service", version="1.0")


@app.on_event("startup")
async def startup():
    if ConfigRegistry._instance is None:
        ConfigRegistry.init("config.yaml")
    setup_logging()
    await init_db()

    app.state.startup_error = None
    try:
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
        from services.ingest import IngestService
        from services.retrieval import RetrievalService
        from storage.chroma_client import ChromaStore

        chroma_store = ChromaStore()
        embedder = Embedder()          # BGE-M3 下载/加载失败会被整体捕获 → degraded
        guard = ResilienceGuard()
        retriever = Retriever(chroma_store, embedder)
        retrieval_service = RetrievalService(
            retriever=retriever, reranker=Reranker(), scanner=InjectionScanner(),
            guard=guard)
        chat_service = ChatService(
            retrieval=retrieval_service, generator=Generator(),
            postprocessor=PostProcessor(), cache=CacheManager(),
            guard=guard, compressor=ConversationCompressor())
        ingest_service = IngestService(chroma_store=chroma_store)

        app.state.chroma_store = chroma_store
        app.state.embedder = embedder
        app.state.guard = guard
        app.state.retrieval_service = retrieval_service
        app.state.chat_service = chat_service
        app.state.ingest_service = ingest_service
        logger.info("api_startup_complete")
    except Exception as exc:
        # Embedder 等重组件加载失败：不崩，health 降级
        app.state.startup_error = str(exc)
        logger.error("api_startup_failed", error=str(exc), exc_info=True)


@app.on_event("shutdown")
async def shutdown():
    pass


app.include_router(router)
