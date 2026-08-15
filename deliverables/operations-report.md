# 运营报表（Operations Report）

> 数据来源：`workspace/cache.db` 的 `request_metrics` 表，按 `source`（chat / eval）分组聚合。
> 覆盖范围：全量 1529 行，时间跨度 `2026-08-15 09:31:20 ~ 2026-08-15 18:30:39`。
> 本报表是**运营报表**（request_metrics 聚合），与评估报表（eval_history 聚合，见 [`eval-history.md`](eval-history.md)）是两张不同表的交付物。

## 核心指标

| source | total_requests | p50_ms | p95_ms | avg_tokens | cache_hit_rate | refusal_rate | pii_redact_total | injection_blocked_total |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| chat | 475 | 2281 | 6002 | 2222.1 | 12.84% | 14.53% | 12 | 12 |
| eval | 1054 | 2167 | 6323 | 2552.3 | 0.95% | 13.19% | 46 | 32 |

指标口径：

- `total_requests`：请求总数（COUNT(*)）。
- `p50_ms` / `p95_ms`：`latency_total` 的 50 / 95 百分位（numpy.percentile，线性插值，单位 ms）。
- `avg_tokens`：`token_total` 平均值（保留 1 位小数）。
- `cache_hit_rate` / `refusal_rate`：百分比口径（`cache_hit`/`refused` 为 TRUE 的请求数 ÷ 总请求数 × 100，ROUND 2），与设计文档 SQL 口径一致。
- `pii_redact_total`：`pii_redact_count` 求和（PII 脱敏总触发次数）。
- `injection_blocked_total`：`injection_blocked` 求和（注入攻击拦截总次数）。

## 与设计文档（§6.2 运营报表 CSV 输出列，designs/rag-service-design.md L2207-2213）的偏离说明

参考规格列：`timestamp, config, total_requests, p50_ms, p95_ms, avg_tokens, cache_hit_rate, refusal_rate, answer_compliance, pii_redact_total, injection_blocked_total`。

以 `request_metrics` 表真实列（PRAGMA table_info 实测）为准，实际产出做了如下取舍：

| 规格列 | 处理 | 原因 |
|--------|------|------|
| `timestamp` | 省略 | 本报表为全量聚合，无时间窗口维度；数据覆盖范围已在报表头部注明 |
| `config` | 以 `source` 替代 | `request_metrics` 表无 `config` 列（仅有 `retrieval_mode` / `source`）；本报表按 `source` 分组，CSV 首列即 `source` |
| `answer_compliance` | 省略 | 该列全表为 NULL（0/1529），运营链路未回填该分数；compliance 指标在评估链路中经 eval_history 落库，不属于 request_metrics 运营口径 |

## 补充：retrieval_mode 分布（最接近 `config` 的检索配置信息）

| source | vector-only | hybrid | hybrid+rerank |
|--------|:---:|:---:|:---:|
| chat | 121 | 126 | 228 |
| eval | 394 | 330 | 330 |

## 运维观察（基于数据，供后续诊断参考）

- **chat 缓存命中率（12.84%）显著高于 eval（0.95%）**：符合预期——eval 链路按设计做了缓存隔离（R8 修复项），确保评估不污染生产缓存；chat 侧自然流量有真实复用。
- **refusal_rate 两分组均约 13-15%**：chat（14.53%）略高于 eval（13.19%），属同一量级；若需进一步归因可结合 `refusal_reason` 分布分析。
- **P95 延迟（6002 / 6323 ms）明显高于 P50（2281 / 2167 ms）**：长尾明显，结合 `timeout`（44 次）、`degraded`（39 次）记录，重模型加载与慢生成是长尾主因；本报表未聚合该两项，如需可扩展。

## 附：CSV

同名 CSV 见 [`operations-report.csv`](operations-report.csv)，列：`source, total_requests, p50_ms, p95_ms, avg_tokens, cache_hit_rate, refusal_rate, pii_redact_total, injection_blocked_total`。
