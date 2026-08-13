# 日志字段字典（Log Field Dictionary）

> RAG QA Service 交付物 — 结构化日志字段定义与样本  
> 关联设计文档: docs/rag-service-design.md §6.2

## 1. 日志体系

- 框架: structlog（JSON 输出）
- 请求追踪: 每请求生成 `request_id`（UUID），经 `structlog.contextvars.bind_contextvars` 绑定，全链路透传
- 日志去向: stdout（structlog PrintLoggerFactory）；eval.sh 做 stdout/stderr 分流 —
  结构化日志写入 `data/logs/eval-<timestamp>.log`，第三方库裸噪音
  （urllib3 警告 / chromadb telemetry / tokenizers）单独存 `*.stderr.log`，不污染主日志

## 2. 通用字段（每条日志必含）

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `timestamp` | ISO 8601 | 日志产生时间（structlog TimeStamper） | `2026-08-13T06:40:27.471725Z` |
| `level` | string | 日志级别 | `info` / `warning` / `error` |
| `event` | string | 事件名 | `retrieval_complete` |
| `module` | string | 产生日志的模块 | `chat` / `retrieval` / `guard` / `scanner` |

## 3. 事件字段字典

### 请求生命周期

| event | 附加字段 | 说明 |
|-------|---------|------|
| `api_startup_complete` | — | 服务启动完成 |
| `api_startup_failed` | `error` | 启动失败（health 降级） |
| `request_start` | `request_id`, `session_id` | 请求进入 |
| `request_complete` | `request_id`, `latency_total_ms` | 请求完成 |

### 检索链路

| event | 附加字段 | 说明 |
|-------|---------|------|
| `retrieval_stage_timeout` | `query` | 检索阶段超时（3s 预算） |
| `rerank_degraded` | `query`, `top_n` | 重排降级（超时/熔断/预热失败） |
| `rerank_warmup_failed` | `query`, `exc_info` | 重排模型预热失败 |

### 安全链路

| event | 附加字段 | 说明 |
|-------|---------|------|
| `injection_detected` | `chunk_id`, `pattern`, `severity`, `action` | 注入扫描命中（action=block/warn） |

### 生成链路

| event | 附加字段 | 说明 |
|-------|---------|------|
| `chat_generation_timeout` | `query` | 生成阶段超时（5s 预算，partial 降级） |

### 入库链路

| event | 附加字段 | 说明 |
|-------|---------|------|
| `ingest_skipped_unchanged` | `source_file`, `doc_hash` | 内容未变跳过 |
| `ingest_complete` | `source_file`, `doc_type`, `version`, `chunks_new`, `chunks_replaced` | 入库完成 |
| `ingest_finalize_failed` | `exc_info` | ingest_log 落库/清缓存失败（非致命） |
| `conflict_detected` | `count`, `samples` | 同标题异内容冲突告警 |

### 韧性链路

| event | 附加字段 | 说明 |
|-------|---------|------|
| `stage_timeout` | `stage`, `timeout` | 通用阶段超时（core.guard） |

## 4. 样本日志

```json
{"module": "scanner", "chunk_id": "8782815c7de440fb982bae2b7f5eef08", "pattern": "(?i)(ignore|disregard|override|forget)\\s+(?:(?:all|previous|above)\\s+){1,2}(instructions?|rules?|prompts?)", "severity": "high", "action": "block", "event": "injection_detected", "level": "warning", "timestamp": "2026-08-13T06:19:27.123456Z"}
```

```json
{"module": "core.guard", "stage": "rerank", "timeout": 2, "event": "stage_timeout", "level": "warning", "timestamp": "2026-08-13T06:40:27.471725Z"}
```

```json
{"module": "retrieval", "query": "公司年假天数是怎么规定的？", "top_n": 5, "event": "rerank_degraded", "level": "warning", "timestamp": "2026-08-13T06:40:27.471825Z"}
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
