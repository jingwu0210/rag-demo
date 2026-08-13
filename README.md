# RAG QA Service

企业内部知识库多轮问答服务（RAG + 生成式 AI）。单实例部署，全配置驱动。

## 快速开始

```bash
# 前提：设置 DeepSeek API Key（用于 LLM 生成）
export DEEPSEEK_API_KEY=<your-key>

./run.sh        # 一键启动 (http://localhost:8000)
                # 首次运行自动完成：依赖安装 → 语料生成 → 入库（下载 BGE-M3 模型）
                # 再次运行检测到知识库非空，跳过引导直接启动
                # NO_BOOTSTRAP=1 ./run.sh 强制跳过引导

./eval.sh       # 一键评估（三配置对比 + 报表 → data/eval/results/）
```

## API 端点

| 端点 | 说明 |
|------|------|
| POST /chat | 多轮问答（query + 可选 session_id） |
| POST /ingest | 文档入库（file + doc_type + version） |
| POST /eval/run | 触发评估任务（后台执行，立即返回 202 + run_id） |
| GET /eval/result | 查询评估结果（?run_id=；缺省返回最近一次） |
| GET /report | 运营报表（request_metrics 聚合 CSV） |
| GET /health | 健康检查 |

## Scripts 用法

| 脚本 | 用途 |
|------|------|
| `scripts/generate_corpus.py` | 生成 10 个 mock 语料文档 → `data/corpus/`（设计文档 §6.4） |
| `scripts/ingest_corpus.py` | 语料入库（OCR/分片/向量化/版本管理） |
| `scripts/verify_corpus.py` | 入库验收（版本替换/OCR/注入样本/过期埋点/双语 6 项） |
| `scripts/generate_test_set.py` | QA 测试集生成（DeepSeek LLM 生成 + 人工修正） |
| `scripts/inspect_db.py` | 数据库查看工具（见下） |

```bash
# 语料
HF_ENDPOINT=https://hf-mirror.com .venv/bin/python scripts/generate_corpus.py
HF_ENDPOINT=https://hf-mirror.com .venv/bin/python scripts/ingest_corpus.py
.venv/bin/python scripts/verify_corpus.py

# 数据查看（data/cache.db 各表）
.venv/bin/python scripts/inspect_db.py                          # 评估汇总
.venv/bin/python scripts/inspect_db.py eval --bad               # 评估异常样本
.venv/bin/python scripts/inspect_db.py eval --runs              # 历史评估列表
.venv/bin/python scripts/inspect_db.py ingest                   # 入库日志
.venv/bin/python scripts/inspect_db.py metrics                  # 请求指标（延迟/token/拒答/安全）
.venv/bin/python scripts/inspect_db.py turns --session <id>     # 指定会话的对话轮次
```

## 架构

- 分层：API (FastAPI) → Service (编排) → Core (引擎) → Storage (ChromaDB/SQLite)
- 检索：vector-only / hybrid / hybrid+rerank 三模式配置切换（RRF k=60 + AdaptiveK）
- 模型：BGE-M3 本地 Embedding/Reranker + DeepSeek Flash API 生成（Adapter 可替换）
- 安全：注入扫描 + Prompt 沙箱 + PII 脱敏 + 拒答三规则
- 观测：structlog JSON 全链路日志 + request_metrics 运营指标

## 配置

所有行为由 `config.yaml` 驱动（100+ 项）。环境变量 `RAG_<key>` 可覆盖。

## 文档

- 架构设计: docs/rag-service-design.md
- 实施计划: docs/2025-08-12-rag-service-plan.md
