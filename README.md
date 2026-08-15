# RAG QA Service

企业内部知识库多轮问答服务（RAG + 生成式 AI）。单实例部署，全配置驱动。

## 快速开始

**唯一外部依赖**：DeepSeek API Key。其余（虚拟环境、Python 依赖、语料生成、模型下载、入库）全部由脚本自举。

```bash
# 前提：设置 DeepSeek API Key（用于 LLM 生成与评估 judge）
export DEEPSEEK_API_KEY=<your-key>

./run.sh        # 一键启动 (http://localhost:8000)
                # 首次运行自动完成：依赖安装 → 语料生成 → 入库（下载 BGE-M3 模型）
                # 再次运行检测到知识库非空，跳过引导直接启动
                # NO_BOOTSTRAP=1 ./run.sh 强制跳过引导

./eval.sh       # 一键评估（三配置对比 + 报表 → workspace/results/）
                # 需先跑过 ./run.sh（venv + 知识库引导）；缺 key/环境/知识库会明确报错
```

**自定义语料（用你自己的文档替代演示语料）**：

```bash
# 1. 把自己公司的文档（支持 pdf / docx / md / txt）放进 assets/corpus/ 目录
#    （可建子目录，如 assets/corpus/员工手册/xxx.pdf）
# 2. 首次运行 ./run.sh 时会自动检测到你的文档并直接入库，
#    不会生成演示语料（空库 + 无用户文档时才生成演示语料）
./run.sh

# 知识库已非空时，后续补充文档：
.venv/bin/python scripts/ingest_corpus.py          # 增量扫描 assets/corpus/ 入库
# 或服务运行中单文件入库：
#   POST /ingest（multipart: file + doc_type + version?）
# 注意：若先跑过演示语料引导，再入库自己的文档，两类内容会混在同一知识库中；
#      想要"纯自己语料"的知识库，请先把文档放入 assets/corpus/ 后再首次运行 ./run.sh。
```

**配套测试集（用自己的语料评估时）**：

预置测试集 `assets/testsets/test_set_archived_v2.json`（80 条，演示语料配套）是针对演示语料设计的问题——用你自己的语料后，这些问题的答案不在你的知识库里，评估会大面积拒答。请为你的语料准备配套测试集（JSON 格式）：

```json
[
  {"question": "你的问题？", "ground_truth": "基于你的文档的参考答案",
   "relevant_chunks": [], "language": "zh", "is_out_of_scope": false}
]
```

放置方式二选一：① 直接替换测试集文件（新语料 baseline 建成后为 `assets/testsets/test_set.json`）；② 存为独立文件并在 config.yaml 改 `eval.test_set_path` 指向它。建议混入 10-20% 知识库外的 out-of-scope 问题（`is_out_of_scope: true`）以测拒答能力。

**镜像 override（海外环境可选）**：脚本默认使用国内镜像（HuggingFace `https://hf-mirror.com`、pip 清华源）。海外环境如镜像不可用，可显式覆盖：

```bash
export HF_ENDPOINT=https://huggingface.co
export PIP_INDEX_URL=https://pypi.org/simple
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
| GET /config | 服务配置（config.yaml 解析 dict，含 RAG_ 环境变量覆盖） |
| GET /rag-gen-ai-service-demo | 演示 UI（无框架单文件工作台，Chat/Ingest/Eval/Report/Maintenance/Config 六页） |

## Scripts 用法

| 脚本 | 用途 |
|------|------|
| `scripts/generate_corpus.py` | 生成 10 个 mock 语料文档 → `assets/corpus/`（设计文档 §6.4） |
| `scripts/ingest_corpus.py` | 语料入库（三模式：增量扫描 / 单文件 / 安全 wipe，见下） |
| `scripts/verify_corpus.py` | 入库验收（版本替换/OCR/注入样本/过期埋点/双语 6 项） |
| `scripts/generate_test_set.py` | QA 测试集生成（DeepSeek LLM 生成 + 人工修正） |
| `scripts/inspect_db.py` | 数据库查看工具（见下） |

```bash
# 语料生成
HF_ENDPOINT=https://hf-mirror.com .venv/bin/python scripts/generate_corpus.py

# 入库 — 增量（默认）：扫描 assets/corpus/ 自动发现新文件
#   新文件→ingested / 已有文件→skipped（hash 相同）/ 同 doc_group 新版本→replaced
HF_ENDPOINT=https://hf-mirror.com .venv/bin/python scripts/ingest_corpus.py

# 入库 — 单文件（新文档零代码改动）
HF_ENDPOINT=https://hf-mirror.com .venv/bin/python scripts/ingest_corpus.py \
    --file assets/corpus/new_policy.pdf --doc-type handbook --version v1.0

# 入库 — 安全 wipe 重灌（chunker 参数/embedding 模型/metadata 格式变更时）
#   自动备份 cache.db → SQLite 表清空 → ChromaDB 集合重建 → 全量重灌
HF_ENDPOINT=https://hf-mirror.com .venv/bin/python scripts/ingest_corpus.py --wipe

# 入库验收
.venv/bin/python scripts/verify_corpus.py

# 数据查看（workspace/cache.db 各表）
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
- 模型：BGE-M3 本地 Embedding/Reranker + DeepSeek v4 Flash API 生成（Adapter 可替换）
- 安全：注入扫描 + Prompt 沙箱 + PII 脱敏 + 拒答三规则
- 观测：structlog JSON 全链路日志 + request_metrics 运营指标

## 配置

所有行为由 `config.yaml` 驱动（100+ 项）。环境变量 `RAG_<key>` 可覆盖。

## 文档

- 架构设计: designs/rag-service-design.md
- 实施计划: designs/2025-08-12-rag-service-plan.md
