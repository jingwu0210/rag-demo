# 评估历史档案（Evaluation History）

> before/after 对比的数据来源。每次评估的完整数据归档于此，评估报表的带时间戳文件存于 `data/eval/results/`。

## 三轮评估总览

| 轮次 | 时间 | 测试集 | 关键修复状态 | run_id |
|------|------|:---:|------|--------|
| R1 基线 | 2026-08-13 早 | 53 条 | 修复前基线（AdaptiveK 分数语义 bug 在册、OOS 漏拒 8 条、sources 截断 500、超时样本污染） | eval_20260813_145525_27789441 |
| R2 中途 | 2026-08-13 午 | 53 条 | R1-R4 已修；但 rerank 超时降级 bug 在册（60 候选×512字符=2248ms>2s） | eval_20260813_164524_748f985c |
| R3 当前 | 2026-08-13 晚 | 65 条 | rerank 截断修复 + 测试集三类区分度样本 + Style 绝对打分 | eval_20260813_182254_9fba2815 |

## R1 基线数据（53 条测试集）

| Config | Faithfulness | Context Precision | Answer Compliance* | Refusal | Style | P50 (ms) | P95 (ms) | Avg Tokens |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| vector-only | 0.6399 | 0.5368 | 0.8113 | 1.0 | — | 1621 | 2533 | 1215 |
| hybrid | 0.6207 | 0.4651 | 0.9623 | 0.8491 | — | 1399 | 3001 | 870 |
| hybrid+rerank | 0.6132 | 0.4884 | 0.9623 | 0.8491 | — | 1462 | 2387 | 886 |

\* R1/R2 的 Answer Compliance 为规则近似语义（有答案且检索到 sources 计 1）；R3 起改为 LLM judge 5 分制 score/5。

## R2 中途数据（53 条测试集，R1-R4 修复后但 rerank 降级未修）

| Config | Faithfulness | Context Precision | Answer Compliance* | Refusal | Style | P50 (ms) | P95 (ms) | Avg Tokens |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| vector-only | 0.8946 | 0.6919 | 1.0 | 1.0 | 0.86 | 1258 | 2035 | 1212 |
| hybrid | 0.8947 | 0.6349 | 1.0 | 1.0 | 0.63 | 1520 | 2163 | 1481 |
| hybrid+rerank | 0.8569 | 0.6647 | 1.0 | 1.0 | 0.71 | 3275 | 4171 | 1192 |

## R3 当前数据（65 条测试集，全部修复后）

| Config | Faithfulness | Context Precision | Answer Compliance | Refusal | Style | P50 (ms) | P95 (ms) | Avg Tokens |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| vector-only | 0.8947 | 0.6714 | 1.0 | 0.9692 | 0.8415 | 1212 | 2006 | 1203 |
| hybrid | 0.8956 | 0.6330 | 1.0 | 0.9692 | 0.8264 | 1161 | 1860 | 1487 |
| hybrid+rerank | 0.9090 | 0.6296 | 1.0 | 0.9692 | 0.8679 | 1993 | 2729 | 1155 |

## 核心对比结论

### R1 → R2（检索质量修复链：AdaptiveK 分数语义 + OOS 分层 + 截断取消）

| 指标 | 变化 | 幅度 |
|------|------|------|
| Faithfulness（hybrid） | 0.6207 → 0.8947 | **+44%** |
| Context Precision（vector） | 0.5368 → 0.6919 | **+29%** |
| Refusal（hybrid） | 0.8491 → 1.0 | **+18%**（OOS 漏拒 8 条清零） |

### R2 → R3（rerank 截断修复 + 测试集区分度 + Style 重设计）

| 指标 | 变化 | 说明 |
|------|------|------|
| Faithfulness（rerank） | 0.8569 → 0.9090 | rerank 不再超时降级，精排真正生效 |
| P95（rerank） | 4171ms → 2729ms | **-35%**（60 候选截断 200 字符后推理 2248→782ms） |
| Style 断崖 | 0.86/0.63/0.71 → 0.84/0.83/0.87 | pairwise 改绝对打分后跨配置可比 |
| Refusal | 1.0 → 0.9692 | 边界模糊样本捕获 2 个真实缺陷（关键词误伤+挖矿漏网） |

## 数据完整性声明

- R1/R2 的 eval_history 明细（per_qa）因 2026-08-13 的 `rm data/cache.db` 操作事故丢失，本档案数据从当时读取的报表内容恢复（聚合值完整，per_qa 明细不可恢复）
- R3 起 eval_history 完整（含 per_qa），报表带时间戳归档不覆盖
- 事故教训见 CLAUDE.md 铁律 10
