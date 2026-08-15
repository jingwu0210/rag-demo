# RAG QA Service 评估报告（R6→R9 before/after）

> **文档定位**：本报告是 assignment 核心交付物，回答两个问题 ——
> （A）三检索配置在多个轮次的指标变化，改进幅度是否 ≥10%；
> （B）评估中发现的关键问题（Issue Diagnosis），基于日志 + 代码给出根因与修复效果。
>
> **数据源**：`deliverables/eval-history.md`（R6/R7/R8 归档）、`workspace/results/eval_report-*.md`（带时间戳原始报表）、`designs/rag-service-design.md` §6.3（指标定义与 Issue Diagnosis 模板）。
> 指标语义、计算方案、达标阈值均以设计文档 §6.3 为准。
>
> **轮次与 run_id 索引**（测试集均为 110 条 baseline，R6-R9 同 `test_set_hash`，直接可比）：

| 轮次 | 时间 | run_id | 关键修复状态 |
|------|------|--------|------|
| R6 | 2026-08-15 下午 | `eval_20260815_153126_066eddcb` | 新语料 baseline 首轮，F1-F4 修复前 |
| R7 | 2026-08-15 夜 | `eval_20260815_173117_b48364fd` | F1-F4 全部修复后（检索质量修复链完成） |
| R8 | 2026-08-16 | `eval_20260816_013749_061408b0` | v1.17 数据失真修复后（judge 关思考 / 截断 / 关键词 / 缓存隔离 / 超时语义） |
| R9（最终轮） | 2026-08-16 | `eval_20260816_022054_4243d245` | v1.17 全量验证 + OOS 软拒 judge 生效 |

---

## 0. 摘要（TL;DR）

- **最终轮 R9 三配置五大指标全部达标**：Faithfulness ≥0.85 / Context Precision ≥0.70 / Answer Compliance ≥0.80 / Refusal Appropriateness ≥0.80 / Style Consistency ≥0.85，三配置逐项核对均通过（见 §2）。
- **两条修复链，改进幅度均 ≥10%**：
  - **R6→R7 检索质量修复链（F1-F4）**：Context Precision 三配置 +10.7~11.4pp；Answer Compliance 三配置 +23.0~26.5pp。
  - **R7→R9 数据失真修复链（v1.17 + OOS 软拒）**：Refusal Appropriateness 三配置 +12.7~13.6pp（0.8545-0.8636 → 0.9909）；OOS 拒答率 0.7222 → 1.0（+27.8pp / 相对 +38.5%）；阶段超时 19/12/8 → 0/0/0。
- **关键教训（数据失真）**：R8 前评估曾出现两类系统性失真 —— judge 思考链吃满 `max_tokens` 导致 Compliance 恒 5（恒 1.0 假象）、全文 rerank 超时导致空检索误拒 45 条。两者修复后指标才真实反映系统能力（详见 issue-diagnosis.md）。
- **选型建议**：`hybrid` 综合最优（Compliance 最高、延迟最低、token 最省）；`vector-only` 检索精度最高（CP/Faith 双第一）且延迟可控；`hybrid+rerank` 在本语料（252 chunks）增益有限、P95 延迟约 2.2×，不宜默认启用（详见 §2.3）。

---

## 1. 评估范围与方法

### 1.1 测试集与可比性

- 测试集：110 条 baseline（含 20% OOS + 三类区分度样本），R6-R9 使用同一测试集，`test_set_hash` 一致，before/after 直接可比。
- 三检索配置：`vector-only`（纯向量）/ `hybrid`（向量 + BM25 + RRF）/ `hybrid+rerank`（hybrid 粗排 + CrossEncoder 精排）。
- 评估走完整生产链路（检索 → 生成 → 后处理），并发度 5（`eval.concurrency=5`）。

### 1.2 指标口径（设计文档 §6.3）

| 指标 | 计算来源 | 达标目标 | 说明 |
|------|---------|:---:|------|
| Faithfulness | Ragas LLM judge | ≥0.85 | 回答断言能否全部在检索 chunk 中找到支撑；OOS/超时样本跳过 |
| Context Precision | Ragas LLM judge | ≥0.70 | 检索 chunk 按排名加权的有效相关性；OOS 样本跳过 |
| Answer Compliance | 自研 LLM judge（6 档归一） | ≥0.80 | 不增、不漏、不改原文；0 分（有答案但未回答）参与均值 |
| Refusal Appropriateness | 纯规则四场景 | ≥0.80 | 正确拒答 / 正确作答 / 误拒 / 漏拒 四类二元判定 |
| Style Consistency | 自研 LLM judge（vs 风格规范绝对打分，全量答案） | ≥0.85 | 话术规范符合度，与检索配置正交 |

- 性能指标：P50/P95 延迟、Avg Tokens、Timeout Rate、Unanswered Rate（refused + timeout + 空答案）。
- 分层视图（v1.13）：`oos_refusal_rate`（OOS 子集拒答正确率，测 OOS 防御）、`normal_refusal_rate`（非 OOS 未误拒占比，测误拒）、未作答三类分解（refused/timeout/空）。

### 1.3 模型代际注记

- R1-R7 评估产物为 `deepseek-chat`（2026-07-24 停服）；R8 起切换 `deepseek-v4-flash` 并显式关闭思考模式。
- **跨模型对比（R7→R8→R9）存在代际差异**，本报告按任务口径以 R7→R9 作为"数据失真修复链"的对比区间，并在逐指标解读中区分"模型切换噪声"与"修复带来的真实变化"。

---

## 2. 最终轮（R9）三配置对比与达标判定

### 2.1 R9 主表（run_id=eval_20260816_022054_4243d245，110 条）

| Config | Faithfulness | Context Precision | Answer Compliance | Refusal Appropriateness | Style Consistency | P50 (ms) | P95 (ms) | Avg Tokens | 千次成本 (¥) |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| vector-only | 0.9414 | **0.8138** | 0.90666 | 0.9909 | 0.8896 | 1279 | 2619 | 3056 | 3.15 |
| hybrid | 0.9289 | 0.7748 | **0.94652** | 0.9909 | **0.8917** | **1157** | **2562** | **2442** | **2.52** |
| hybrid+rerank | 0.9393 | 0.7978 | 0.90698 | 0.9909 | 0.8875 | 3303 | 5636 | 2443 | 2.53 |

> 千次成本 = (avg_prompt/1e6 × ¥1.00 + avg_completion/1e6 × ¥2.00) × 1000，deepseek-v4-flash 单价（config `llm.price_input_per_m` / `price_output_per_m`，设计文档 §7.5）。由 R9 实测 token 均值换算，`hybrid` 最低（¥2.52/千次）。

### 2.2 R9 分层视图（三配置一致）

| Config | OOS 拒答率（OOS 子集） | 正常业务拒答正确率（误拒检测） | 未作答分解 refused/timeout/空 |
|--------|:---:|:---:|:---:|
| vector-only | **1.0** | 0.9891 | 14/0/0 |
| hybrid | **1.0** | 0.9891 | 14/0/0 |
| hybrid+rerank | **1.0** | 0.9891 | 14/0/0 |

> 解读：OOS 子集全部正确拒答（1.0）；非 OOS 子集误拒 1 条（正常业务拒答正确率 0.9891）；未作答 14 条全部为规则层硬拒（refused），无超时、无空答案。14 条 = 13 条 OOS 硬拒 + 1 条非 OOS 误拒；另有软拒答 OOS 样本由 judge 判为正确处理，故 OOS 拒答率达 1.0。

### 2.3 逐指标达标判定

| 指标（目标值） | vector-only | hybrid | hybrid+rerank | 判定 |
|------|:---:|:---:|:---:|:---:|
| Faithfulness（≥0.85） | 0.9414 | 0.9289 | 0.9393 | ✅ 全部达标 |
| Context Precision（≥0.70） | 0.8138 | 0.7748 | 0.7978 | ✅ 全部达标 |
| Answer Compliance（≥0.80） | 0.90666 | 0.94652 | 0.90698 | ✅ 全部达标 |
| Refusal Appropriateness（≥0.80） | 0.9909 | 0.9909 | 0.9909 | ✅ 全部达标 |
| Style Consistency（≥0.85） | 0.8896 | 0.8917 | 0.8875 | ✅ 全部达标 |

### 2.4 选型结论（R9 实测）

- **质量最优**：
  - 检索精度：vector-only（CP 0.8138 / Faith 0.9414 双第一）。
  - 答案合规：hybrid（Compliance 0.94652，三配置最高）。
- **性能最优**：hybrid（P50 1157ms / P95 2562ms / Avg Tokens 2442 全最低）。
- **hybrid+rerank 评价**：与 vector-only/hybrid 相比质量提升不显著（CP +0.5pp、Compliance 反而低 4pp），但 P95 5636ms 约为 hybrid 的 2.2 倍。根因见 R7 残余 #4 —— 语料仅 252 chunks、粗排候选池 30 覆盖全库约 12%，CrossEncoder 精排的增益空间有限；在更大语料上方能体现价值。
- **建议**：默认 `hybrid`；对"检索精度优先"场景可切 `vector-only`；`hybrid+rerank` 建议在语料扩容后重新评估再启用。

---

## before/after 对比

> 对比口径：同一 110 条测试集，`test_set_hash` 一致。改进幅度以**百分点（pp）**标注（与 `deliverables/eval-history.md` 的标注口径一致），≥10pp 视为达标。

### A.1 R6→R7 检索质量修复链（F1-F4）

**修复内容**：F1 空格 PDF 根治（块级双字体渲染）+ F2 OOS 口径显式排除 + F3 PII pattern 扩展（卡号/国际电话脱敏）+ F4 judge 盲区约束 + `max_chunks` 10 + `expire` 开启。

**R6（修复前）→ R7（修复后）对比**：

| 指标 | vector-only | hybrid | hybrid+rerank |
|------|:---:|:---:|:---:|
| Context Precision | 0.6558 → 0.7694（**+11.4pp**） | 0.6440 → 0.7563（**+11.2pp**） | 0.6624 → 0.7697（**+10.7pp**） |
| Answer Compliance | 0.6699 → 0.9000（**+23.0pp**） | 0.6865 → 0.9517（**+26.5pp**） | 0.6490 → 0.9116（**+26.3pp**） |
| Faithfulness | 0.9145 → 0.9138（±0） | 0.8971 → 0.9132（+1.6pp） | 0.9025 → 0.9147（+1.2pp） |
| Refusal | 0.8636 → 0.8636（±0） | 0.8636 → 0.8636（±0） | 0.8545 → 0.8545（±0） |
| Style | 0.8857 → 0.8914（+0.6pp） | 0.8667 → 0.8724（+0.6pp） | 0.8768 → 0.8731（-0.4pp） |
| P95 (ms) | 3122 → 3513 | 2670 → 3378 | 4268 → 4883 |

**结论**：
- **CP 三配置提升 10.7~11.4pp（≥10%）**：R6 归因显示 16 题×3 配置 CP=0（空格 PDF 召回失败，占 CP 损失 50.8%），F1 修复后该异常清零。
- **Compliance 三配置提升 23.0~26.5pp（≥10%）**：F2 显式排除 OOS 样本（38 个正确拒答不再被判 0）+ F3 脱敏 rubric + F4 盲区约束共同作用。
- **Faithfulness 在 R6 已达标（≈0.90）**，F 修复链未引入幻觉回归（仅微幅波动）。
- **代价**：P95 上升约 300-600ms —— `max_chunks 10` 增加上下文量（Avg Chunks 5.68-9.05），属于"用延迟换召回完整性"的已接受权衡。

**R6 在册异常 → R7 验证**（eval-history 归档）：

| # | 异常 | 根因 | 修复 | R7 验证 |
|---|------|------|------|:---:|
| 1 | 空格 PDF 召回失败（16 题×3 配置 CP=0） | 生成器 CJK 字体缺陷：china-s 渲染拉丁字符插空格，BGE-M3 嵌入失效 | F1 块级双字体渲染 | ✅ 清零 |
| 2 | PII 取值题 judge 偏差 + 卡号/国际电话明文回显 | Ragas relevance judge 与 PII 防御冲突；pattern 未覆盖银行卡/国际电话 | F3 pattern 扩展 + judge rubric | ✅ 脱敏生效 |
| 3 | 排序靠后 + 旧版 chunk 干扰 | legacy 排现行标准前；答案 chunk 常落第 3-8 位 | expire 开启 + max_chunks 10 | ✅ 改善 |
| 4 | OOS 正确拒答被判 0（compliance -0.10） | 大语料下 RefusalCheck 空结果层失效，38 个 OOS 样本 refused=False 漏过滤 | F2 显式排除 OOS | ✅ 修正 |
| 5 | judge 上下文盲区"编造"误判（-0.05） | judge 参考文档仅检索 top-k，条款不存在≠语料不存在 | F4 盲区约束 | ✅ 修正 |
| 6 | 拒答机制对半相关召回敏感 | 大语料检索不再空 → 拒答链路退化 | 挂账 §9.2（系统级待解） | ⏳ |

### A.2 R7→R9 数据失真修复链（v1.17 + OOS 软拒）

**修复内容**：judge/generator/Ragas 统一关闭思考模式（`thinking:disabled`）、rerank 输入截断 300 字符 + `retrieval.timeout` 5→7s、安全/PII 关键词扩展 + 注入检测扫 query、eval 缓存隔离（`source=eval` 跳过缓存读写）、超时语义区分（超时 ≠ 查无信息）、拒答不进缓存。

**R7（deepseek-chat，失真修复前）→ R9（v4-flash，最终轮）对比**：

| 指标 | vector-only | hybrid | hybrid+rerank |
|------|:---:|:---:|:---:|
| Refusal Appropriateness | 0.8636 → 0.9909（**+12.7pp**） | 0.8636 → 0.9909（**+12.7pp**） | 0.8545 → 0.9909（**+13.6pp**） |
| Faithfulness | 0.9138 → 0.9414（+2.8pp） | 0.9132 → 0.9289（+1.6pp） | 0.9147 → 0.9393（+2.5pp） |
| Context Precision | 0.7694 → 0.8138（+4.4pp） | 0.7563 → 0.7748（+1.9pp） | 0.7697 → 0.7978（+2.8pp） |
| Answer Compliance | 0.9000 → 0.9067（+0.7pp） | 0.9517 → 0.9465（-0.5pp） | 0.9116 → 0.9070（-0.5pp） |
| Style | 0.8914 → 0.8896（-0.2pp） | 0.8724 → 0.8917（+1.9pp） | 0.8731 → 0.8875（+1.4pp） |
| P95 (ms) | 3513 → 2619 | 3378 → 2562 | 4883 → 5636 |
| 阶段超时（count） | 19 → 0 | 12 → 0 | 8 → 0 |

**结论**：
- **Refusal 三配置提升 12.7~13.6pp（≥10%）**：300 字符截断 + `timeout 7s` 消除 rerank 超时（19/12/8 → 0），`hybrid+rerank` 误拒 45 条 → 14 条；超时样本从 Refusal 计算中排除（"没来得及搜"不再当"查无信息"）；OOS 软拒 judge 将"软拒答"计入正确（详见 issue-diagnosis.md）。
- **Compliance 由假象恢复真实**：切换 v4-flash 后首轮评估曾出现"judge 恒打 5 分 → Compliance 恒 1.0"的失真假象（未归档，为诊断中间态）；关思考后 R8/R9 回到 0.90-0.95 真实值，与 R7（deepseek-chat 实测 0.90-0.95）基本持平 —— 说明跨模型切换后 Compliance 仍稳定达标（详见 issue-diagnosis.md）。
- **P95 注记**：vector-only / hybrid 的 P95 在数据失真修复后反而下降（-894ms / -816ms，超时消除 + 模型提速）；`hybrid+rerank` P95 5636ms 仍显著偏高，是 CrossEncoder 在 MPS 上串行推理的固有代价，与超时修复不冲突（见 §3）。

**R8 数据失真修复链（6 条根因 + 修复）**：

| # | 失真 | 根因 | 修复 | 效果 |
|---|------|------|------|------|
| 1 | Compliance 恒 5 | v4-flash 思考模式 + `max_tokens=100` 被思考链吃满 → judge 输出退化 | judge/generator/Ragas 关思考 | Compliance 恒 1.0（假象）→ 0.90-0.95（有区分度） |
| 2 | hybrid+rerank 过度拒答（45 条） | 全文 rerank ~5s × 5 并发 MPS 争抢 → 粗排超时 → 空 sources → 误拒 | 300 字符截断 + timeout 7s | 拒答 45 → 14，refusal 0.8545 → 0.9909 |
| 3 | OOS 漏拒（0.17） | top1_sim 0.45 阈值区分不了"话题相似 OOS" | 安全/PII 关键词 + 注入扫 query | OOS 拒答率 0.7222（硬拒）→ 1.0（软拒后） |
| 4 | eval 缓存污染 | `cache_key=query|mode` 无 source 维度 | `source=eval` 跳过缓存读写 | eval 不再读到旧答案 |
| 5 | 超时误判漏拒 | 检索超时 → 空 sources → low_confidence（语义错） | 超时语义（返回"系统繁忙"）+ refusal 排除超时 | timeout 19/12/8 → 0 |
| 6 | 拒答污染缓存 | 误拒结果 `cache.put` 达 TTL | 拒答不进缓存 | 误拒不再复现 |

---

## 3. 残余问题与后续建议

### 3.1 R7 已知残余状态（R9 后复核）

| # | 残余问题 | 证据 | R9 状态 |
|---|---------|------|:---:|
| 1 | 同文档内精细条款排序靠后 | 答案条款在长文档内部排不进 top7 | ⏳ 待修（P1h 权重调优实验） |
| 2 | PII 取值题 judge 判定偏差 | 5 条 PII 题检索全中仍 CP=0 | ⏳ 待修（评估层口径，类 F2） |
| 3 | 短文档跨文档压制残量 | 低分组含短文档 59% vs 高分组 26% | ⏳ 待修（P1g 或接受） |
| 4 | 理论梯度未现（vector≈rerank>hybrid CP） | 语料 252 chunks、候选池覆盖全库 12% | 📝 已如实写入 §2.4 选型结论 |
| 5 | 口语短问句拒答（P2c 代价） | "公司年假有几天？" top1=0.236 被滤空 | ⏳ P2d 待做（用户暂缓） |
| 6 | 模型代际 | deepseek-chat → v4-flash | ✅ 已切换并验证（R8/R9 达标） |

### 3.2 后续建议

1. **rerank 延迟**：R9 `hybrid+rerank` P95 5636ms 仍是短板（CrossEncoder 在 MPS 上串行推理的固有成本）。语料扩容后先复测再决定是否默认启用；如保留，可评估更小精排模型或 batch 化。
2. **同文档条款排序**（残余 #1）：`top_n` 已调至 7，后续按 P1h 权重调优实验推进。
3. **PII 取值 judge 口径**（残余 #2）：PII 题检索全中仍 CP=0，建议评估层单列口径（类 F2 显式处理），避免误伤检索链路评价。

---

## 4. 数据完整性声明

- R1/R2 的 per_qa 明细因 2026-08-13 `rm data/cache.db` 事故丢失（聚合值完整）；R3/R4 起完整。
- R4 的 compliance/unanswered_rate 为旧口径，v1.10 起语义变更，跨口径对比需注明；本报告涉及轮次（R6-R9）均为新口径。
- R5 起测试集为 `test_set_archived_v2.json`（80 条）；本报告 R6-R9 为 110 条新语料 baseline 测试集，`test_set_hash` 一致，轮次间直接可比。
- 模型代际：R1-R7 为 deepseek-chat（2026-07-24 停服）；R8 起为 deepseek-v4-flash（显式关闭思考模式）。R7→R9 对比存在代际差异，报告已逐指标标注。
- 路径注记：v1.15 起目录分层（`data/` → `workspace/`），历史记录中 `data/cache.db` 对应现 `workspace/cache.db`。
- 原始报表：`workspace/results/eval_report-20260816_023104.md`（R9）、`eval_report-20260816_014823.md`（R8），以及 `eval_report-20260815_174443.md`（R7）、`eval_report-20260815_154632.md`（R6）。
