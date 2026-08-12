# RAG QA Service

企业内部知识库多轮问答服务（RAG + 生成式 AI）。单实例部署，全配置驱动。

## 快速开始

```bash
./run.sh        # 启动服务 (http://localhost:8000)
./eval.sh       # 一键评估（三配置对比 + 报表）
```

## API 端点

| 端点 | 说明 |
|------|------|
| POST /chat | 多轮问答（query + 可选 session_id） |
| POST /ingest | 文档入库（file + doc_type + version） |
| POST /eval/run | 触发评估任务 |
| GET /eval/result | 查询评估结果 |
| GET /report | 运营报表（CSV） |
| GET /health | 健康检查 |

## 架构

- 分层：API (FastAPI) → Service (编排) → Core (引擎) → Storage (ChromaDB/SQLite)
- 检索：vector-only / hybrid / hybrid+rerank 三模式配置切换（RRF k=60 + AdaptiveK）
- 模型：BGE-M3 本地 Embedding/Reranker + DeepSeek Flash API 生成（Adapter 可替换）
- 安全：注入扫描 + Prompt 沙箱 + PII 脱敏 + 拒答三规则
- 观测：structlog JSON 全链路日志 + request_metrics 运营指标

## 配置

所有行为由 `config.yaml` 驱动（100+ 项）。环境变量 `RAG_<key>` 可覆盖。

## 文档

- 架构设计: docs/2025-08-12-rag-service-design.md
- 实施计划: docs/2025-08-12-rag-service-plan.md
