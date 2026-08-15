# 日志字段字典（Log Field Dictionary）

> RAG QA Service 交付物 — 结构化日志字段定义与样本  
> 关联设计文档: designs/rag-service-design.md §6.2

## 1. 日志体系

- 框架: structlog（JSON 输出）
- 格式: **头部四字段平铺 + 业务数据分组嵌套**（zap / OpenTelemetry 风格）— 固定键序 `timestamp → level → event → module`，业务数据按语义分组（request / query / cache / retrieval / rerank / llm / answer / refusal / pii / summary / chunks）
- 请求追踪: 每请求生成 `request_id`（UUID），API 层 `bind_contextvars` 绑定，Service 层经 `get_request_id()` 读取放入 `request.id`，全链路透传
- 日志去向: stdout（structlog PrintLoggerFactory）；eval.sh 做 stdout/stderr 分流 — 结构化日志写入 `workspace/logs/eval-<timestamp>.log`，第三方库裸噪音（urllib3 警告 / chromadb telemetry / tokenizers）单独存 `*.stderr.log`，不污染主日志
- 日志与数据的职责分工: 日志 = 事件流（轻量、流式、可 grep）；DB = 数据仓库（turns / request_metrics 表存 query/answer 全文与指标，可 SQL 聚合）

## 2. 通用头部字段（每条日志必含，顺序固定）

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `timestamp` | ISO 8601 | 日志产生时间（行首固定位） | `2026-08-13T16:45:24.558Z` |
| `level` | string | 日志级别（行首第二固定位） | `info` / `warning` / `error` |
| `event` | string | 事件名 | `generation_complete` |
| `module` | string | 产生日志的模块 | `chat` / `retrieval` / `api` / `guard` / `scanner` |

## 3. 事件字段字典

### 请求生命周期（module: api）

| event | 分组字段 | 说明 |
|-------|---------|------|
| `chat_request_start` | `request{id, session_id}`, `query{text(截断200), language, doc_type, history_turns}` | 请求进入：问题原文（截断）+ 预处理结果 + 历史轮数 |
| `chat_request_end` | `request{id, session_id}`, `summary{total_latency_ms, tokens_total, cache_hit, refused, timeout, degraded}` | 请求完成：一次请求完整画像 |
| `chat_concurrency_limit` | — | 并发超限（429） |
| `chat_invalid_session_recreated` | — | 无效 session_id → 新会话替代 |

### 检索链路（module: retrieval）

| event | 分组字段 | 说明 |
|-------|---------|------|
| `retrieval_complete` | `request{id}`, `retrieval{mode, coarse_candidates, final_chunks, top1_score, vector_top1_sim, doc_type_filter, injection_blocked, latency_ms}`, `chunks[{heading_path, score, source_file}×前5]` | 检索完成：中间态全貌（"送 LLM 前几个 chunk"在此）+ 前 5 条 chunk 明细 |
| `rerank_complete` | `request{id}`, `rerank{candidates, kept, top1_score, latency_ms}` | 精排完成（仅 hybrid+rerank 模式） |
| `retrieval_stage_timeout` | `query` | 检索阶段超时（3s 预算） |
| `rerank_degraded` | `query`, `top_n` | 精排降级（超时/熔断/预热失败） |

### 缓存链路（module: chat）

| event | 分组字段 | 说明 |
|-------|---------|------|
| `cache_check` | `request{id}`, `cache{hit, key(前8位)}` | 缓存查询结果 |

### 生成链路（module: chat）

| event | 分组字段 | 说明 |
|-------|---------|------|
| `generation_complete` | `request{id}`, `llm{provider, model, tokens_prompt, tokens_completion, latency_ms}`, `answer{preview(脱敏后前200), truncated, length}` | 生成完成：Token 消耗 + 回答预览（全文落 turns 表） |
| `chat_generation_timeout` | `query` | 生成超时（5s，partial 降级） |

### 安全链路

| event | 分组字段 | 说明 |
|-------|---------|------|
| `injection_detected` | `chunk_id`, `pattern`, `severity`, `action` | 注入扫描命中（module: scanner） |
| `refusal_triggered` | `request{id}`, `refusal{reason, signal}` | 拒答触发：原因 + 判定所用置信度值（module: chat） |
| `pii_redacted` | `request{id}`, `pii{redactions}` | PII 脱敏触发（仅 count>0 时，module: chat） |

### 入库链路（module: ingest）

| event | 分组字段 | 说明 |
|-------|---------|------|
| `ingest_skipped_unchanged` | `source_file`, `doc_hash` | 内容未变跳过 |
| `ingest_complete` | `source_file`, `doc_type`, `version`, `chunks_new`, `chunks_replaced` | 入库完成 |
| `conflict_detected` | `count`, `samples` | 同标题异内容冲突告警 |

### 韧性链路（module: core.guard）

| event | 分组字段 | 说明 |
|-------|---------|------|
| `stage_timeout` | `stage`, `timeout` | 通用阶段超时 |

## 4. 样本日志（真实抽取自 workspace/logs）

> 样本来源：
> - `chat_request_start` / `cache_check` / `retrieval_complete` / `generation_complete` / `chat_request_end` / `refusal_triggered` / `pii_redacted` ← `workspace/logs/run-20260816-015507.log`（chat 服务日志，UTC 时间戳，本地时区 UTC+8）
> - `rerank_complete` ← `workspace/logs/eval-20260816_022043.log`（chat 服务本次运行未走 hybrid+rerank 模式，精排事件仅评估链路触发）
>
> 说明：
> - 实际日志在 §3 分组字段之外，末尾另附平铺 `request_id` 字段（与 `request.id` 同值，兼容旧查询格式）。
> - 前 4 条样本（`chat_request_start` → `cache_check` → `retrieval_complete` → `generation_complete` → `chat_request_end`）同属一次真实请求，`request.id` 均为 `72e655238f4b4405b95b42ff7ccf2224`，可验证全链路透传。
> - 超长字段（如 `answer.preview`）在展示时截断，以 `…` 标注，非字段本身被修改。

### 4.1 chat_request_start（请求进入）

```json
{"timestamp": "2026-08-15T17:55:54.244581Z", "level": "info", "event": "chat_request_start", "module": "api", "request": {"id": "72e655238f4b4405b95b42ff7ccf2224", "session_id": "497864d32f4a4a9d8848cdaa7703411c"}, "query": {"text": "公司明天的股价会涨吗？", "language": "zh", "doc_type": "general", "history_turns": 4}, "request_id": "72e655238f4b4405b95b42ff7ccf2224"}
```

- `request.id`：本次请求 UUID（32 位十六进制，全链路追踪键）
- `query.text`：问题原文（字典 §3 约定截断 200 字，此样本 11 字未触发截断）
- `query.language`：语言检测结果 `zh`
- `query.doc_type`：文档类型过滤 `general`
- `query.history_turns`：携带的历史轮数 `4`
- 尾部平铺 `request_id`：与 `request.id` 同值

### 4.2 cache_check（缓存查询）

```json
{"timestamp": "2026-08-15T17:55:54.248596Z", "level": "info", "event": "cache_check", "module": "chat", "request": {"id": "72e655238f4b4405b95b42ff7ccf2224"}, "cache": {"hit": false, "key": null}, "request_id": "72e655238f4b4405b95b42ff7ccf2224"}
```

- `cache.hit`：`false` = 未命中；命中时置 `true`
- `cache.key`：缓存键前 8 位；`null` = 本次未生成缓存键（未命中态）

### 4.3 retrieval_complete（检索完成）

```json
{"timestamp": "2026-08-15T17:55:54.420001Z", "level": "info", "event": "retrieval_complete", "module": "retrieval", "request": {"id": "72e655238f4b4405b95b42ff7ccf2224"}, "retrieval": {"mode": "vector-only", "coarse_candidates": 10, "final_chunks": 10, "top1_score": 0.4873, "vector_top1_sim": 0.4873, "doc_type_filter": "general", "injection_blocked": 0, "latency_ms": 158}, "chunks": [{"heading_path": "Compliance Guide > 8. Insider Trading and Securities Compliance", "score": 0.4873, "source_file": "compliance-guide.en.md"}, {"heading_path": "员工手册 > 37. 员工建议与创新", "score": 0.475, "source_file": "employee-handbook.cn.md"}, {"heading_path": "员工手册 > 17. 员工发展与晋升", "score": 0.4711, "source_file": "employee-handbook.cn.md"}, {"heading_path": "员工手册 > 9. 培训发展", "score": 0.4685, "source_file": "employee-handbook.cn.md"}, {"heading_path": "员工手册 > 5. 薪酬福利 > 5.1 薪酬", "score": 0.4635, "source_file": "employee-handbook.cn.md"}], "request_id": "72e655238f4b4405b95b42ff7ccf2224"}
```

- `retrieval.mode`：`vector-only`（此样本未走 hybrid / rerank，故无 rerank 阶段事件）
- `coarse_candidates` → `final_chunks`：粗召回 10 → 送入生成 10
- `top1_score` / `vector_top1_sim`：均 `0.4873` — vector 模式分数语义为余弦（0-1），与 rerank 的 CrossEncoder 无界分数不同，勿跨模式比较阈值
- `injection_blocked`：注入扫描拦截数 `0`
- `chunks`：字典 §3 约定仅打印前 5 条明细（heading_path / score / source_file），此样本完整展示

### 4.4 rerank_complete（精排完成）

```json
{"timestamp": "2026-08-15T18:27:30.717377Z", "level": "info", "event": "rerank_complete", "module": "retrieval", "request": {"id": null}, "rerank": {"candidates": 20, "kept": 7, "top1_score": 0.0328, "latency_ms": 4467}}
```

- 来源：`workspace/logs/eval-20260816_022043.log`（评估上下文未绑定 request_id，故 `request.id` 为 `null`；chat 服务链路该字段为 UUID）
- `rerank.candidates` → `kept`：精排输入 20 → 输出 7
- `rerank.top1_score`：`0.0328` — CrossEncoder 无界分数，语义与 vector 余弦不同（见 §3 检索分数语义）
- `rerank.latency_ms`：精排耗时 4467ms

### 4.5 generation_complete（生成完成）

```json
{"timestamp": "2026-08-15T17:55:56.487552Z", "level": "info", "event": "generation_complete", "module": "chat", "request": {"id": "72e655238f4b4405b95b42ff7ccf2224"}, "llm": {"provider": "deepseek", "model": "deepseek-v4-flash", "tokens_prompt": 3111, "tokens_completion": 162, "latency_ms": 2066}, "answer": {"preview": "根据现有文档，我无法给出确切答案。公司股价的涨跌属于市场行为，内部知识库中不包含任何股价预测信息。\n\n同时需要提醒您，根据《Compliance Guide》第8章…（展示截断）", "truncated": true, "length": 280}, "request_id": "72e655238f4b4405b95b42ff7ccf2224"}
```

- `llm.provider` / `llm.model`：`deepseek` / `deepseek-v4-flash`
- `llm.tokens_prompt`（3111）+ `tokens_completion`（162）：本次生成 Token 消耗（原文 `\n` 为真实换行符）
- `llm.latency_ms`：生成耗时 2066ms
- `answer.preview`：脱敏后回答预览（样本中按展示截断，`…` 处非真实日志内容）；`truncated: true` 表示非全文（全文落 turns 表）
- `answer.length`：回答总长 280

### 4.6 refusal_triggered（拒答触发）

```json
{"timestamp": "2026-08-15T18:03:36.773181Z", "level": "info", "event": "refusal_triggered", "module": "chat", "request": {"id": "fc542cbf07ab42cba404cc98758cafdb"}, "refusal": {"reason": "low_confidence", "signal": 0.4248}, "request_id": "fc542cbf07ab42cba404cc98758cafdb"}
```

- `refusal.reason`：`low_confidence`（低置信拒答）
- `refusal.signal`：判定所用置信度信号 `0.4248`
- 非每请求输出 — 仅在拒答触发时出现

### 4.7 pii_redacted（PII 脱敏触发）

```json
{"timestamp": "2026-08-15T18:07:12.827418Z", "level": "info", "event": "pii_redacted", "module": "chat", "request": {"id": "e5bf46a9331841fab84987595345ebbb"}, "pii": {"redactions": 1}, "request_id": "e5bf46a9331841fab84987595345ebbb"}
```

- `pii.redactions`：本次请求 PII 脱敏数量 `1`
- 字典 §3 约定：仅在 count>0 时输出，故正常请求大多无此事件（本样本为真实触发记录）

### 4.8 chat_request_end（请求完成）

```json
{"timestamp": "2026-08-15T17:55:56.510750Z", "level": "info", "event": "chat_request_end", "module": "api", "request": {"id": "72e655238f4b4405b95b42ff7ccf2224", "session_id": "497864d32f4a4a9d8848cdaa7703411c"}, "summary": {"total_latency_ms": 2241, "tokens_total": 3273, "cache_hit": false, "refused": false, "timeout": false, "degraded": false}, "request_id": "72e655238f4b4405b95b42ff7ccf2224"}
```

- `summary.total_latency_ms`：整请求耗时 2241ms（含检索 158 + 生成 2066 + 其余开销）
- `summary.tokens_total`：prompt + completion 合计 3273
- `summary.cache_hit` / `refused` / `timeout` / `degraded`：本次请求各状态标记（全 `false`）

> 覆盖说明：8 个正常路径事件在现有日志中均有真实样本，无缺失。`injection_detected` 事件不在本次覆盖清单内（属异常路径，日志中亦未捕获到触发记录）。

## 5. 运营指标（非日志，SQLite request_metrics 表）

与日志互补的量化指标存储：`data/cache.db` 的 `request_metrics` 表（每请求一行）：

| 字段 | 说明 |
|------|------|
| `latency_retrieval/rerank/generation/total` | 分阶段延迟（ms） |
| `token_prompt/completion/total` | Token 用量 |
| `retrieval_mode` | 检索模式 |
| `cache_hit` / `refused` / `refusal_reason` | 缓存命中 / 拒答 |
| `timeout` / `degraded` | 超时 / 降级标记 |
| `pii_redact_count` / `injection_blocked` | 安全计数 |
| `faithfulness_score` / `context_precision` / `answer_compliance` | 评估回填 |

聚合查询见设计文档 §6.2（GET /report 端点直接输出 CSV）。
