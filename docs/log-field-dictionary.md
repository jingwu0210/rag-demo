# 日志字段字典（Log Field Dictionary）

> RAG QA Service 交付物 — 结构化日志字段定义与样本  
> 关联设计文档: docs/rag-service-design.md §6.2

## 1. 日志体系

- 框架: structlog（JSON 输出）
- 格式: **头部四字段平铺 + 业务数据分组嵌套**（zap / OpenTelemetry 风格）— 固定键序 `timestamp → level → event → module`，业务数据按语义分组（request / query / cache / retrieval / rerank / llm / answer / refusal / pii / summary / chunks）
- 请求追踪: 每请求生成 `request_id`（UUID），API 层 `bind_contextvars` 绑定，Service 层经 `get_request_id()` 读取放入 `request.id`，全链路透传
- 日志去向: stdout（structlog PrintLoggerFactory）；eval.sh 做 stdout/stderr 分流 — 结构化日志写入 `data/logs/eval-<timestamp>.log`，第三方库裸噪音（urllib3 警告 / chromadb telemetry / tokenizers）单独存 `*.stderr.log`，不污染主日志
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

## 4. 样本日志（新格式，头部平铺 + 分组嵌套）

```json
{"timestamp": "2026-08-13T16:45:24.558Z", "level": "info", "event": "chat_request_start", "module": "api", "request": {"id": "a1b2c3d4", "session_id": "s-abc123"}, "query": {"text": "公司年假天数是怎么规定的？", "language": "zh", "doc_type": "handbook", "history_turns": 0}}
```

```json
{"timestamp": "2026-08-13T16:45:24.562Z", "level": "info", "event": "cache_check", "module": "chat", "request": {"id": "a1b2c3d4"}, "cache": {"hit": false, "key": null}}
```

```json
{"timestamp": "2026-08-13T16:45:25.203Z", "level": "info", "event": "retrieval_complete", "module": "retrieval", "request": {"id": "a1b2c3d4"}, "retrieval": {"mode": "hybrid+rerank", "coarse_candidates": 30, "final_chunks": 5, "top1_score": 0.8962, "vector_top1_sim": 0.746, "doc_type_filter": "general", "injection_blocked": 0, "latency_ms": 320}, "chunks": [{"heading_path": "第一章 休假制度", "score": 0.8962, "source_file": "employee_handbook_v1.1.pdf"}]}
```

```json
{"timestamp": "2026-08-13T16:45:25.813Z", "level": "info", "event": "rerank_complete", "module": "retrieval", "request": {"id": "a1b2c3d4"}, "rerank": {"candidates": 30, "kept": 5, "top1_score": 0.8962, "latency_ms": 610}}
```

```json
{"timestamp": "2026-08-13T16:45:28.450Z", "level": "info", "event": "generation_complete", "module": "chat", "request": {"id": "a1b2c3d4"}, "llm": {"provider": "deepseek", "model": "deepseek-chat", "tokens_prompt": 812, "tokens_completion": 156, "latency_ms": 3200}, "answer": {"preview": "根据《员工手册》第一章第1.1节，年假天数按照司龄计算：司龄 1-3 年每年 10 天...", "truncated": true, "length": 156}}
```

```json
{"timestamp": "2026-08-13T16:45:28.500Z", "level": "info", "event": "chat_request_end", "module": "api", "request": {"id": "a1b2c3d4", "session_id": "s-abc123"}, "summary": {"total_latency_ms": 4700, "tokens_total": 968, "cache_hit": false, "refused": false, "timeout": false, "degraded": false}}
```

```json
{"timestamp": "2026-08-13T16:46:10.000Z", "level": "info", "event": "refusal_triggered", "module": "chat", "request": {"id": "b2c3d4e5"}, "refusal": {"reason": "low_confidence", "signal": 0.12}}
```

```json
{"timestamp": "2026-08-13T16:50:00.000Z", "level": "warning", "event": "injection_detected", "module": "scanner", "chunk_id": "8782815c", "pattern": "(?i)(ignore|disregard)...", "severity": "high", "action": "block"}
```

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
