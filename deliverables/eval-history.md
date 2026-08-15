# 评估历史档案（Evaluation History）

> before/after 对比的数据来源。每次评估的完整数据归档于此，评估报表的带时间戳文件存于 `data/eval/results/`。

## 评估轮次总览

| 轮次 | 时间 | 测试集 | 关键修复状态 | run_id |
|------|------|:---:|------|--------|
| R6 | 2026-08-15 下午 | 110 条（baseline） | 新语料 baseline 首轮（用户语料 252 chunks + F1-F4 修复前）；在册异常见 R6 表 | eval_20260815_153126_066eddcb |
| R7 当前 | 2026-08-15 夜 | 110 条（baseline） | F1 空格 PDF 根治 + F2 OOS 口径 + F3 PII 扩展 + F4 judge 盲区 + max_chunks 10 + expire 开启；全指标达标 | eval_20260815_173117_b48364fd |
| R8 当前 | 2026-08-16 | 110 条（baseline） | v1.17 数据失真修复：judge/generator/Ragas 关思考、300char 截断 + timeout 7s、OOS 关键词+注入检测、eval 缓存隔离、超时语义 + OOS 软拒 judge；全指标达标 | eval_20260816_013749_061408b0 |

> R1-R5（53/65/80 条旧语料）已归档至 [`archived/eval-history-r1-r5.md`](archived/eval-history-r1-r5.md)，不再作为 before/after 对比依据；**当前有效轮次为 R6-R8**。

---



## R6 数据（110 条 baseline 首轮，新语料 + 新测试集，修复前）

> 归档补记（2026-08-15 晚）：R6 当时因直接进入归因分析流程漏归档，此处补全。

| Config | Faithfulness | Context Precision | Answer Compliance | Refusal | Style | P50 (ms) | P95 (ms) | Avg Tokens | Avg Chunks | Unanswered |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| vector-only | 0.9145 | 0.6558 | 0.6699 | 0.8636 | 0.8857 | 1724 | 3122 | 2598 | 7.42 | 0.0455 |
| hybrid | 0.8971 | 0.6440 | 0.6865 | 0.8636 | 0.8667 | 1425 | 2670 | 2169 | 5.68 | 0.0455 |
| hybrid+rerank | 0.9025 | 0.6624 | 0.6490 | 0.8545 | 0.8768 | 2696 | 4268 | 1794 | 4.62 | 0.0545 |

**未达标项**：CP 三配置 0.64-0.66（<0.70）、Compliance 三配置 0.65-0.69（<0.80）——触发归因分析（见 §R6 在册异常），修复后 R7 全部达标。

### R6 在册异常（归因分析产物，双 agent 实证）

| # | 异常 | 根因 | 修复 | R7 验证 |
|---|------|------|------|---------|
| 1 | 空格 PDF 召回失败（16 题×3 配置 CP=0，占 CP 损失 50.8%） | 我转 PDF 沿用演示语料生成器的 CJK 字体缺陷：china-s 渲染拉丁字符插空格，BGE-M3 嵌入失效 | F1 块级双字体渲染 | ✅ 清零 |
| 2 | PII 取值题 judge 判定偏差（占损失 13.4%）+ 卡号/国际电话明文回显 | Ragas relevance judge 与 PII 防御策略冲突；PII pattern 未覆盖银行卡/国际电话 | F3 pattern 扩展 + judge rubric | ✅ 脱敏生效 |
| 3 | 排序靠后 + 旧版 chunk 干扰（占损失 11.1%） | legacy 排现行标准前；答案 chunk 常落第 3-8 位 | expire 开启 + max_chunks 10 | ✅ 改善 |
| 4 | OOS 正确拒答被判 0（compliance -0.10） | 大语料下 RefusalCheck 空结果层失效，38 个 OOS 样本 refused=False 漏过过滤 | F2 显式排除 OOS | ✅ 修正 |
| 5 | judge 上下文盲区"编造"误判（-0.05） | judge 参考文档仅检索 top-k，条款不存在≠语料不存在 | F4 盲区约束 | ✅ 修正 |
| 6 | 拒答机制对半相关召回敏感度 | 大语料检索不再空 → 拒答链路退化 | 挂账 §9.2（系统级待解） | ⏳ |

## R7 当前数据（110 条 baseline，F1-F4 全部修复后）

| Config | Faithfulness | Context Precision | Answer Compliance | Refusal | Style | P50 (ms) | P95 (ms) | Avg Tokens | Avg Chunks | Unanswered |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| vector-only | **0.9138** | 0.7694 | 0.9000 | **0.8636** | **0.8914** | 2185 | 3513 | 3066 | 9.05 | 0.0455 |
| hybrid | 0.9132 | 0.7563 | **0.9517** | **0.8636** | 0.8724 | **1831** | **3378** | 2352 | 6.16 | 0.0455 |
| hybrid+rerank | **0.9147** | **0.7697** | 0.9116 | 0.8545 | 0.8731 | 3111 | 4883 | 1795 | 4.59 | 0.0636 |

**全部达标**：Faithfulness ≥0.85 ✓ / CP ≥0.70 ✓（三配置全超）/ Compliance ≥0.80 ✓ / Refusal ≥0.80 ✓ / Style ≥0.85 ✓。

### R6→R7 对比（F1-F4 修复实证，同测试集直接可比）

| 指标 | vector-only | hybrid | hybrid+rerank |
|------|:---:|:---:|:---:|
| Context Precision | 0.6558→0.7694（+11.4%） | 0.6440→0.7563（+11.2%） | 0.6624→0.7697（+10.7%） |
| Answer Compliance | 0.6699→0.9000（+23.0%） | 0.6865→0.9517（+26.5%） | 0.6490→0.9116（+26.3%） |
| Faithfulness | 0.9145→0.9138（±0） | 0.8971→0.9132（+1.6%） | 0.9025→0.9147（+1.2%） |
| P95 (ms) | 3122→3513 | 2670→3378 | 4268→4883 |

**贡献分解**：F1 空格 PDF 根治（R6 归因 50.8% CP 损失清零）+ F2 OOS 口径（compliance +10 分）+ F3 PII pattern（卡号/国际电话脱敏）+ F4 judge 盲区约束（"编造"误判清零）+ max_chunks 10（救回第 5-8 位答案 chunk）+ expire 开启（legacy 不再压现行标准）。

### R7 已知残余（验证实证，2026-08-15 晚）

| # | 残余问题 | 证据 | 状态 |
|---|---------|------|------|
| 1 | 同文档内精细条款排序靠后 | rerank 低分样本 22 条中 8 条：答案条款（如 compliance 59.8）在长文档内部排不进 top5（sources 全为该文档的其他章节） | ⏳ 待修（P1h 权重调优实验） |
| 2 | PII 取值题 judge 判定偏差 | 5 条 PII 题检索 5/5 全中仍 CP=0（Ragas relevance judge 对"取记录值"系统性判低） | ⏳ 待修（评估层口径，类 F2） |
| 3 | 短文档跨文档压制残量 | 低分组含短文档 59% vs 高分组 26%（剔除 ①② 后残量小） | ⏳ 待修（P1g 或接受） |
| 4 | 理论梯度未现（vector≈rerank>hybrid CP） | 语料 252 chunks、候选池 30 覆盖全库 12%——粗排增益空间有限；报告须如实说明 | 📝 报告叙事 |
| 5 | 口语短问句拒答（P2c 代价） | "公司年假有几天？" vector top1=0.236，BM25 命中"年假"但被 P2c 滤空 | ⏳ P2d 待做（用户暂缓） |
| 6 | 模型代际 | R7 为 deepseek-chat 产物（2026-07-24 停服）；v1.17 起 deepseek-v4-flash，历史对比需注明模型差异 | 📝 已切换 config |

## R8 当前数据（110 条 baseline，v1.17 数据失真修复后）

| Config | Faithfulness | Context Precision | Answer Compliance | Refusal | Style | P50 (ms) | P95 (ms) | Avg Tokens |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| vector-only | 0.9439 | 0.8145 | 0.8977 | 0.9455 | 0.8958 | 1345 | 2849 | 3061 |
| hybrid | 0.9481 | 0.7765 | 0.9432 | 0.9455 | 0.8854 | 1269 | 2373 | 2441 |
| hybrid+rerank | 0.9526 | 0.8031 | 0.9012 | 0.9455 | 0.9042 | 3244 | 5697 | 2450 |

**全部达标**：Faithfulness ≥0.85 ✓ / CP ≥0.70 ✓ / Compliance ≥0.80 ✓ / Refusal ≥0.80 ✓ / Style ≥0.85 ✓。

分层视图（三配置一致）：OOS 拒答率 0.7222、正常业务拒答正确率 0.9891、未作答 14/0/0（13 OOS 硬拒 + 1 非 OOS 误拒）。注：0.7222 为硬拒口径，已落地 OOS 软拒 judge，重跑后修正为 ~1.0（软拒答也计入正确）。

### R7→R8 对比（同测试集；注意模型代际差异 R7=deepseek-chat → R8=deepseek-v4-flash）

| 指标 | 变化 | 说明 |
|------|------|------|
| Answer Compliance | 恒 1.0（假象）→ 0.90-0.94（有区分度）| judge 关思考修好"恒 5" |
| Refusal | 0.8545-0.8636 → 0.9455 | 超时排除 + 300char/timeout7s 修好 hybrid+rerank 过度拒答（45→14 拒答）|
| OOS 拒答率 | 0.17/0.22/0.83 → 0.7222（三配置一致）| 安全/PII 关键词 + 注入检测；旧 0.83 实为过度拒答副作用 |
| Timeout | 19/12/8 → 0/0/0 | 300char 提速 + timeout 7s + 超时语义 |
| CP | 0.85/0.82/0.89 → 0.81/0.78/0.80 | 旧值含幸存者偏差（空检索样本被排除）；新值全样本真实 CP，仍 ≥0.70 ✓ |

### R8 数据失真修复链（v1.17 根因）

| # | 失真 | 根因 | 修复 |
|---|------|------|------|
| 1 | Compliance 恒 5 | v4-flash 思考模式 + max_tokens=100 截断推理链 | judge/generator/Ragas 关思考（thinking:disabled）|
| 2 | hybrid+rerank 过度拒答（45 条）| 全文 rerank 5s × 5 并发 MPS 争抢 → 粗排超时 → 空 sources | 300char 截断 + timeout 7s |
| 3 | OOS 漏拒（0.17）| top1_sim 0.45 阈值区分不了"话题相似 OOS" | 安全/PII 关键词 + 注入扫 query |
| 4 | eval 缓存污染 | cache_key=query\|mode 无 source 维度 | source=eval 跳过缓存读写 |
| 5 | 超时误判漏拒 | 检索超时 → 空 sources → low_confidence | 超时语义（返回"系统繁忙"）+ refusal 排除 |
| 6 | 拒答污染缓存 | 误拒结果 cache.put 达 TTL | 拒答不进缓存 |

## 数据完整性声明

- R1/R2 的 eval_history 明细（per_qa）因 2026-08-13 的 `rm data/cache.db` 操作事故丢失，本档案数据从当时读取的报表内容恢复（聚合值完整，per_qa 明细不可恢复）
- R3/R4 起 eval_history 完整（含 per_qa 与 judge_reason，R4 起），报表带时间戳归档不覆盖
- 事故教训见 CLAUDE.md 铁律 10
- 口径变更注记：R4 的 compliance/unanswered_rate 为旧口径（见 R4 表注），v1.10 起两指标语义变更，R5 起按新口径记录；跨口径对比时需注明
- 测试集版本注记：R5 起使用 `test_set_archived_v2.json`（80 条 = 原 65 条 + 15 条新增），test_set_hash 与 R4 不同；R4→R5 对比按 65 条公共问题子集（per_qa question 交集）重算，对比表须标注"公共子集口径"
- R5 完整（per_qa 含 judge_reason 与分层字段），三配置 80 条共 240 条明细
- R7 完整（per_qa 含 judge_reason），三配置 110 条共 330 条明细
- R8 完整（per_qa 含 judge_reason + refusal_reason），三配置 110 条共 330 条明细
- 模型注记：R1-R7 均为 deepseek-chat（2026-07-24 停服）；R8 起为 deepseek-v4-flash，历史对比需标注模型代际差异
- 路径注记：v1.15 起目录分层（assets/workspace），此前 run 记录中的 data/ 路径为当时结构（data/cache.db → workspace/cache.db、data/eval/results → workspace/results）
