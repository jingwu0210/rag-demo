# 样本日志（Sample Logs）

> 8 个正常路径事件各一条真实 JSON 样本，独立文档（与字段字典分开）。原始样本文件：`sample-logs/sample.log`。

## 事件样本（8 个正常路径事件）

> 独立样本文件：`sample-logs/sample.log`（8 事件各一条原始 JSON，可直接查看/解析）。以下为同批样本的逐条标注。
>
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

