# 检索三模式实现机制与已知问题台账

> 更新：2026-08-15（v1.0）。本文档固化三模式检索链路的**当前实现机制**与**已知问题**，作为后续调优的单一依据。参数以 `config.yaml` 为唯一真源，本文数值为当前默认值快照。

## 一、三模式完整实现机制

### 1.1 vector-only

```
query → BGE-M3 编码 → chroma 余弦检索 top20
  → where 过滤（is_active + expire $gte）
  → AdaptiveK.apply(mode="vector-only")
      ├─ 阈值过滤：score ≥ 0.45（余弦尺度，绝对语义）
      ├─ 全低于阈值 → 保底前 min_chunks=3
      └─ 截断到 max_chunks=10
  → InjectionScanner → 送 LLM
```

- 无融合、无 rerank
- 最终输入 chunk 数：动态 [3, 10]

### 1.2 hybrid

```
query → 向量 top20 + BM25 top20
  → RRF 加权融合：score = 1.0/(60+向量rank) + 0.8/(60+BM25rank)
  → P2c/P2d 过滤：vec_sim ≥ 0.45 保留；否则 BM25 强命中（rank≤3 且分数>0）豁免
  → AdaptiveK.apply(mode="hybrid")
      ├─ 阈值过滤：score ≥ 0.0164（RRF 尺度，"至少一路排第一"理论值 1/61）
      ├─ 保底 min_chunks=3
      └─ 截断 max_chunks=10
  → InjectionScanner → 送 LLM
```

- 无 rerank
- 最终输入 chunk 数：动态 [3, 10]

### 1.3 hybrid+rerank

```
query → 向量/BM25 粗排候选池 top30（top_k 20 × candidates_multiplier 1.5）
  → RRF 加权融合（同 hybrid）
  → P2c/P2d 过滤（同 hybrid）
  → skip_adaptive（粗排不截断，全量候选给 rerank）
  → CrossEncoder 全量打分（输入截断 300 字符——v1.18，见 §3.1）
  → P1h 二次融合：final = 1/(60+粗排rank) + 1/(60+rerank rank)
  → final 降序硬截断 top_n=7
  → InjectionScanner → 送 LLM
```

- 最终输入 chunk 数：固定 top_n=7

### 1.4 分数语义契约（三个尺度，绝不跨尺度比较）

| 尺度 | 值域 | 用途 |
|---|---|---|
| 余弦 | 0-1 | vector 阈值、P2c/P2d 过滤、拒答置信度（vector_top1_sim 旁路） |
| RRF | ~0.016-0.033 | hybrid 阈值、P1h final |
| CrossEncoder | 无界 | 仅 rerank 内部排序 |

> 血泪教训（R2）：曾用余弦 0.45 阈值杀光 RRF 分数（~0.03 全部低于 0.45），hybrid 只剩 3 条保底。此后按 mode 分离阈值尺度。

## 二、演进决策链（简要）

| 决策 | 触发问题 | 状态 |
|---|---|---|
| P1h 二次融合 | R6 rerank 排序失真（通用章节+36% 挤出精确文档） | ✅ R7 验证 |
| P2a RRF 加权（v=1.0/b=0.8） | BM25 无关命中稀释 | ✅ |
| P2c 向量阈值过滤（0.45） | hybrid 噪声（chunks 7.72→5.03） | ✅ 但有代价（见 P2d） |
| P2d BM25 强命中豁免（rank≤3） | 口语短问句向量分崩被滤空 | ✅ 刚修复 |
| R8 并发互斥+worker5 | rerank 首触 47.8s | ✅ R5 验证 |
| R13 max_chunks 8→10 + expire 开启 | 答案 chunk 落第 5-8 位、legacy 压现行标准 | ✅ R7 验证 |
| R14 top_n 5→7 | 同文档多 chunk 争位子 | ⏳ 待 R8 轮验证 |

## 三、已知问题台账

### 3.1 同文档 chunk 竞争（R7 残余 #1，最重要）

- **现象**：6 条 CP<0.5 样本，答案条款在长文档（compliance 75 chunks / handbook 86 chunks）内部，但检索 top5 全是同文档的**其他** chunk，答案 chunk 排不进前列
- **机制**：大文档切 75 个语义高度相似的 chunk，rerank 难以区分"泛相关"与"答案所在"，答案 chunk 混在 74 个相似邻居里排名优势不明显
- **R8 轮深挖（2026-08-15）**：定位到真凶——**rerank 输入截断 200 char 切掉了答案句**（非 rerank 模型问题）：
  - gift register 答案 chunk（"59. Compliance Calendar"）答案句在**第 658 char**（chunk 总长 926）
  - 截断 200/400/600 char 下答案 chunk 排名第 14/18/8 名（分数 0.0001）；**全文下第 1 名（分数 0.8091）**
  - 根因：200 char 截断是 R2 在"小 chunk 语料"下的未实测假设（"黄金信号在 chunk 开头"），扩到真实语料后 chunk 变长 5-8 倍，假设失效（铁律 6 又一案例）
- **决策（A 方案）**：全文 rerank（`input_truncate_chars=0` 不截断）+ timeout 2→5s + 候选保持 30
  - 候选不能降：报销时限答案 chunk 粗排第 24 名，候选 15/20 会漏召回
  - 代价：30 候选全文 ~5s，P95 预期 7-8s（10s SLA 内但余量收紧）
  - **被否的两阶段方案**：阶段 1 短截断快筛会先把答案 chunk 筛掉（它在短截断下排第 14 名），阶段 2 全文看不到——自相矛盾
- **v1.18 修正（2026-08-16）**：A 方案全文 ~5s × 5 并发在 MPS 上争抢 → 粗排超时 → 空检索 → 误拒 45 条（hybrid+rerank Refusal 0.8545）。改为 `input_truncate_chars=300` 折中：rerank ~1.5s 使 5 并发挤进 10s SLA（实测 5/5 达标），CP 0.79 仍 ≥0.70（全文 0.86 但 43% 空检索失败，300 字符仅 2%）。gift register 答案句在 658 char 仍会被 300 截断，但该损失换回了 5 并发正确性——性能与精度的新平衡点。
- **性质**：大文档精细条款检索是 RAG 公认难点；性能-精度硬权衡

### 3.2 理论梯度未现（R7 残余 #4）

- **现象**：CP vector 0.7694 ≈ rerank 0.7697 > hybrid 0.7563；Faith 三组几乎无差异
- **预期**：vector < hybrid < rerank（BM25 增益 + rerank 精排）
- **真实约束**：语料 252 chunks，rerank 候选池 30 覆盖全库 12%——粗排已捞得不错时 rerank 增量空间有限
- **性质**：报告叙事项（如实说明），非缺陷

### 3.3 短文档跨文档压制残量（R7 残余 #3）

- **现象**：低分组含短文档 59% vs 高分组 26%（剔除 PII 与同文档竞争后残量小）
- **候选**：P1g 文档级归一化（未做）

### 3.4 PII 脱敏格式覆盖（R7 残余修复中）

- **现象**：R7 发现带格式的卡号（`4111 1111 1111 1111`）与国际电话（`+1 (415) 555-2671`）明文回显
- **根因**：初版 bankcard/intl_phone pattern 只匹配纯数字串，不匹配空格分组/括号格式
- **修复**：R15 更新 pattern（intl_phone 含括号、bankcard 覆盖空格分组），待重跑验证
- **残余风险**：数字格式枚举仍有盲区（其他分隔符如 `/`、`.`

### 3.5 拒答机制对半相关召回敏感度（R6 挂账）

- **现象**：大语料下 OOS 问题检索不再为空（总能捞到半相关 chunk），RefusalCheck 空结果层失效，38 个 OOS 样本 refused=False
- **现状**：评估口径已修（F2），系统级拒答退化待解
- **方向**：后置识别模型回避式输出补标 refused、或提高半相关召回的拒答敏感度

### 3.6 PII 取值题 judge 判定偏差（R7 残余 #2，已修）

- **现象**：检索 5/5 全中仍 CP=0（Ragas relevance judge 对"取记录值"系统性判低）
- **修复**：`expect_refusal_or_redaction` 题跳过 Ragas（与 OOS 同待遇），该类质量由 compliance/refusal 衡量

### 3.7 模型代际（R7 残余 #6）

- R1-R7 为 deepseek-chat 产物（2026-07-24 停服）；v1.17 起 deepseek-v4-flash，历史对比需标注模型差异
- 新模型基线评估待重跑

## 四、参数速查（当前 config 快照）

| 参数 | 值 | 说明 |
|---|---|---|
| vector.top_k | 20 | 向量粗排候选 |
| bm25.top_k | 20 | BM25 粗排候选 |
| fusion.rrf_k | 60 | RRF 常数 |
| fusion.vector_weight / bm25_weight | 1.0 / 0.8 | RRF 加权 |
| fusion.vector_sim_threshold | 0.45 | P2c 向量硬过滤 |
| fusion.bm25_exempt_rank | 3 | P2d BM25 强命中豁免 |
| adaptive.min_score | 0.45 | vector 模式阈值（余弦） |
| adaptive.hybrid_min_score | 0.0164 | hybrid 模式阈值（RRF） |
| adaptive.min_chunks / max_chunks | 3 / 10 | AdaptiveK 动态截断界 |
| reranker.top_n | 7 | rerank 模式最终硬截断 |
| reranker.candidates_multiplier | 1.5 | rerank 候选池放大（30；报销时限答案粗排第24，不可降） |
| reranker.input_truncate_chars | 300 | rerank 输入截断（v1.18：300 字符折中，CP 0.79 ≥0.70 + 5 并发达标） |
| reranker.timeout | 5 | 阶段预算（300 字符 30 候选 ~1.5s） |
| expire.enabled / grace | true / 90 | 过期过滤 |
