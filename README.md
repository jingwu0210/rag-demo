# RAG QA Service

企业内部知识库多轮问答服务（RAG + 生成式 AI）。单实例部署，全配置驱动，检索三模式（vector-only / hybrid / hybrid+rerank）可切换。

**唯一外部依赖**：DeepSeek API Key。其余（虚拟环境、Python 依赖、模型下载、演示语料生成、入库）全部由脚本自举。

---

## 快速开始

```bash
# 前提：设置 DeepSeek API Key（用于 LLM 生成与评估 judge）
export DEEPSEEK_API_KEY=<your-key>

./run.sh        # 一键启动 → http://localhost:8000
                #   首次运行：装依赖 → 下载 BGE-M3/reranker 模型 → 语料入库 → 启动
                #   空库引导：assets/corpus/ 有你的文档 → 直接入库；无 → 生成演示语料
                #   再次运行：知识库非空，跳过引导，交互式询问 wipe/增量入库
                #   NON_INTERACTIVE=1 ./run.sh 跳过全部交互询问（CI/自动化）

./eval.sh       # 一键评估（三配置对比 + before/after 报表 → workspace/results/）
                #   前置检查：key / venv / 知识库非空 / 模型缓存，缺一即明确报错
                #   评估前自动清缓存（只清 cache_entries，不动评估历史）
                #   预计 10-15 分钟（300 字符截断 + judge 关思考后）
```

**用你自己的语料评估**：把文档（pdf / docx / md / txt）放进 `assets/corpus/`（可建子目录）再首次运行 `./run.sh`——空库时检测到你的文档就直接入库、**不生成演示语料**。配套测试集需换成针对你语料的问题（预置测试集 `assets/testsets/` 是演示语料配套），否则评估会大面积拒答：

```json
[
  {"question": "你的问题？", "ground_truth": "基于你的文档的参考答案",
   "relevant_chunks": [], "language": "zh", "is_out_of_scope": false}
]
```

建议混入 10-20% 知识库外的 out-of-scope 问题（`is_out_of_scope: true`）以测拒答能力。测试集路径可在 `config.yaml` 的 `eval.test_set_path` 改。

**镜像 override（海外环境可选）**：脚本默认国内镜像（HuggingFace `https://hf-mirror.com`、pip 清华源），可显式覆盖：

```bash
export HF_ENDPOINT=https://huggingface.co
export PIP_INDEX_URL=https://pypi.org/simple
```

---

## 交付物（Deliverables）

| # | 交付物 | 链接 |
|---|--------|------|
| 2.1 | 一键评估脚本 | [eval.sh](./eval.sh) |
| 2.2 | 评估报告（before/after 对比） | [evaluation-report.html](https://jingwu0210.github.io/rag-demo/deliverables/eval-reports/evaluation-report.html) |
| 2.3 | 日志字段字典 + 样本日志 | [deliverables/logs/](deliverables/logs/) |
| 2.4 | Issue Diagnosis（4 案例） | [issue-diagnosis.html](https://jingwu0210.github.io/rag-demo/deliverables/diagnosis/issue-diagnosis.html) |
| 2.5 | 运营报表 | [operations-report.html](https://jingwu0210.github.io/rag-demo/deliverables/op-reports/operations-report.html) |
| 2.6 | 演示页面 | 在线 [http://localhost:8000/rag-gen-ai-service-demo](http://localhost:8000/rag-gen-ai-service-demo)（需启动服务） <br>静态 [demo.html](https://jingwu0210.github.io/rag-demo/deliverables/demo/demo.html) (服务不启动查看截图样例) |

> HTML 版为最终交付形态（同设计系统，明暗双主题）；同名 `.md` 为源稿。演示页的在线链接需先 `./run.sh` 启动服务。HTML 报告链接指向 GitHub Pages（`https://jingwu0210.github.io/rag-demo/`），需在 Settings → Pages 启用「Deploy from a branch（main / root）」后生效。

---

## 最终基准（R9）

最终轮 `deepseek-v4-flash`（显式关思考），110 条测试集 × 3 检索配置，5 并发。五指标全达标：

| Config | Faith<br>(≥0.85) | CP<br>(≥0.70) | Compliance<br>(≥0.80) | Refusal<br>(≥0.80) | Style<br>(≥0.85) | P50 | P95 |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| vector-only | 0.9414 ✅ | 0.8138 ✅ | 0.9067 ✅ | 0.9909 ✅ | 0.8896 ✅ | 1279ms | 2619ms |
| hybrid | 0.9289 ✅ | 0.7748 ✅ | **0.9465** ✅ | 0.9909 ✅ | **0.8917** ✅ | **1157ms** | **2562ms** |
| hybrid+rerank | 0.9393 ✅ | 0.7978 ✅ | 0.9070 ✅ | 0.9909 ✅ | 0.8875 ✅ | 3303ms | 5636ms |

**选型建议**：`hybrid` 综合最优（Compliance 最高、延迟与 token 最低）；`vector-only` 检索精度最高（CP / Faith 双第一）且延迟可控；`hybrid+rerank` 在本语料（252 chunks）增益有限、P95 约 2.2×，建议语料扩容后再启用。完整对比见 [evaluation-report.html](https://jingwu0210.github.io/rag-demo/deliverables/eval-reports/evaluation-report.html)。

---

## 设计概述

### API 端点

| 端点 | 说明 |
|------|------|
| POST `/chat` | 多轮问答（query + 可选 session_id） |
| POST `/ingest` | 文档入库（file + doc_type + version） |
| GET `/eval/result` | 查询评估结果（`?run_id=`；缺省返回最近一次） |
| GET `/report` | 运营报表（request_metrics 聚合 CSV） |
| GET `/health` | 健康检查 |
| GET `/config` | 服务配置（config.yaml 解析 dict，含 `RAG_` 环境变量覆盖） |
| GET `/rag-gen-ai-service-demo` | 演示 UI（无框架单文件工作台，Chat/Ingest/Eval/Report/Maintenance/Config 六页） |

### 架构分层

```
API (FastAPI) → Service (编排) → Core (引擎) → Storage (ChromaDB / SQLite)
```

- **检索**：vector-only / hybrid / hybrid+rerank 三模式配置切换（RRF k=60 + AdaptiveK 动态截断 + CrossEncoder 精排）
- **生成**：DeepSeek v4 Flash API（Adapter 可替换，显式关思考模式）
- **安全**：注入扫描 + Prompt 沙箱 + PII 脱敏 + 拒答三规则（注入 / 空检索 / 低置信度）
- **观测**：structlog JSON 全链路日志 + request_metrics 运营指标 + eval_history 评估历史

### 目录布局

```
rag-demo/
├── api/            # FastAPI 应用（routes / schemas / static 演示 UI）
├── core/           # 核心引擎（retriever / embedder / reranker / generator / scanner / …）
├── services/       # 编排层（chat / retrieval / ingest）
├── storage/        # 存储（chroma_client 向量库 / sqlite_client 三表）
├── eval/           # 评估（runner 三配置对比 / report 报表 / test_set 加载）
├── scripts/        # 运维脚本（入库 / 验收 / 测试集生成 / 数据查看）
├── assets/         # 版本化资产进 git（corpus 语料源 / testsets 测试集 / chroma 预置索引）
├── workspace/      # 机器状态 git 忽略（ocr / logs / results / cache.db）
├── deliverables/   # 交付物（报告 / 日志 / 诊断 / demo）
├── designs/        # 设计文档（规格真源）
├── config.yaml     # 全量配置（100+ 项，改配置 = 改行为）
├── run.sh          # 一键启动
└── eval.sh         # 一键评估
```

### 技术选型

| 环节 | 选择 | 理由 |
|------|------|------|
| 文档解析 | PyMuPDF + PaddleOCR | PDF 速度 + 中文 OCR 63.2% SOTA（扫描件） |
| 切片 | 分层语义切片 | 中文友好，尊重标题→段落→句子结构 |
| Embedding | BGE-M3 | 唯一中英双语 + 长文本 8192 + 多语言单空间 |
| Reranker | BGE-Reranker-v2-m3 | 与 BGE-M3 同系列，本地零成本 |
| 向量库 | ChromaDB | 嵌入式 + 原生 where 过滤 + CRUD，满足单实例 |
| LLM | DeepSeek v4 Flash | 延迟最低（10s 硬约束安全边际最大）+ 128K 窗口；备选通义千问 / 智谱 |
| 缓存 | SQLite（L1 精确匹配） | 零额外服务，WAL 支持 5 路并发 |

完整量化论证见 `designs/rag-service-design.md` §7。

---

## Scripts 用法

所有脚本经 `.venv/bin/python` 执行，首次需先跑过 `./run.sh`（建 venv + 下模型）。

| 脚本 | 用途 | 命令 |
|------|------|------|
| `scripts/ingest_corpus.py` | 语料入库（增量 / 单文件 / wipe） | `.venv/bin/python scripts/ingest_corpus.py` |
| `scripts/verify_corpus.py` | 入库验收（版本替换 / OCR / 注入 / 过期 / 双语 6 项） | `.venv/bin/python scripts/verify_corpus.py` |
| `scripts/generate_test_set.py` | QA 测试集生成（DeepSeek LLM + 人工修正） | `.venv/bin/python scripts/generate_test_set.py` |
| `scripts/inspect_db.py` | 数据库查看（评估 / 入库 / 指标 / 会话） | `.venv/bin/python scripts/inspect_db.py` |

入库子命令：

```bash
# 增量（默认）：扫描 assets/corpus/ 自动发现新文件（hash 相同 skipped / 新版本 replaced）
.venv/bin/python scripts/ingest_corpus.py

# 单文件（新文档零代码改动）
.venv/bin/python scripts/ingest_corpus.py --file assets/corpus/new_policy.pdf \
    --doc-type handbook --version v1.0

# 安全 wipe 重灌（chunker 参数 / embedding 模型 / metadata 格式变更时）
#   自动备份 cache.db → 表清空 → Chroma 集合重建 → 全量重灌
.venv/bin/python scripts/ingest_corpus.py --wipe
```

数据查看子命令：

```bash
.venv/bin/python scripts/inspect_db.py eval --runs          # 历史评估列表
.venv/bin/python scripts/inspect_db.py eval --bad           # 评估异常样本
.venv/bin/python scripts/inspect_db.py ingest               # 入库日志
.venv/bin/python scripts/inspect_db.py metrics              # 请求指标（延迟/token/拒答/安全）
.venv/bin/python scripts/inspect_db.py turns --session <id> # 指定会话的对话轮次
```

> 演示语料生成（`scripts/generate_corpus.py`）已内聚到 `run.sh` 空库引导流程，无需手动调用。

---

## 配置

所有行为由 `config.yaml` 驱动（100+ 项）。环境变量 `RAG_<key>` 可覆盖（下划线转点号，如 `RAG_RETRIEVAL__MODE=hybrid`）。

---

## 文档

- 架构设计（全流程规格真源）: `designs/rag-service-design.md`
- 检索三模式实现机制与已知问题台账: `designs/retrieval-mechanism.md`
- 前端 UI 设计: `designs/frontend-ui-design.md`
