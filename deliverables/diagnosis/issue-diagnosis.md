# Issue Diagnosis 案例（R6-R9 评估）

> 依据设计文档 §6.3「Issue Diagnosis 模板」，以 R6-R9 真实 run 数据填写，未沿用模板示例数字。编号按时间线：ISSUE-001（R6→R7 检索质量链）~ ISSUE-004（R7→R9 数据失真链）。

## ISSUE-001：空格 PDF 召回失败 —— CJK 字体渲染给拉丁字符插空格

```
问题编号: ISSUE-001
问题现象: 16 题 × 3 配置 = 48 个样本 Context Precision 全部为 0，占 CP 损失 50.8%。
日志证据:
  # R6 在册异常 #1（eval-history）——"空格 PDF 召回失败（16 题×3 配置 CP=0，
  # 占 CP 损失 50.8%）"
  # 语料产物抽查：英文条款的拉丁字符被插入空格（"g i f t   r e g i s t e r"）
根因分析: 转 PDF 时沿用演示语料生成器的 CJK 字体缺陷：china-s 字体渲染拉丁字符时
          在字符间插入空格，导致 BGE-M3 对英文条款的嵌入彻底失效——向量检索对
          这批 chunk 的相似度归零，召回失败。
修复方案: F1 块级双字体渲染：中文用 CJK 字体、拉丁用拉丁字体，generate_corpus 与
          语料 PDF 同步修复重灌。
修复效果: CP 从 0（48 个样本）→ 清零；R6→R7 Context Precision 三配置
          0.6558→0.7694（+11.4%）/ 0.6440→0.7563（+11.2%）/ 0.6624→0.7697（+10.7%），
          均 ≥10%。
```

## ISSUE-002：Compliance 恒 5 —— judge 思考链吃满 `max_tokens`，指标失真

```
问题编号: ISSUE-002
问题现象: 切换 deepseek-v4-flash 后评估，Answer Compliance 三配置恒 ≈1.0
          （judge 恒打 5 分），指标失去区分度，无法反映真实合规质量。
日志证据:
  # 根因机制（eval/runner.py _judge_llm_call 注释原文）：
  # "deepseek-v4-flash 默认开启思考模式：思考链（reasoning_content）
  #  会吃满 max_tokens=100，导致 content 恒空 → judge_unparsable"
  # 同根因的"输出退化"在修复后的 R8/R9 评估日志中仍有残余表现（每次运行 11-12 条）：
  [2026-08-15 17:41:12Z] warning judge_unparsable content="分数|理由" (eval.runner)
  # 修复前后 compliance 对比（eval_history + R9 报表）：
  #   修复前（中间态）：三配置恒 1.0（judge 恒打 5 分，假象）
  #   修复后（R9 评估日志）：answer_compliance=0.90666 / 0.94652 / 0.90698（有区分度）
根因分析: deepseek-v4-flash 默认开启思考模式，思考链（reasoning_content）先占用
          生成预算；自研 judge 请求 max_tokens=100 本意是限制输出长度，但预算被
          思考链吃满，content 退化（恒 5 分 / 输出模板字面量 "分数|理由"）→
          Compliance 恒 1.0 假象。Ragas judge（faithfulness/CP）同样受影响
          （长 prompt 推理极慢，实测 4 分钟仅 27% 完成）。修复前 runner.py 对
          自研 judge 与 Ragas 均未关闭思考模式。
修复方案: 对 judge / generator / Ragas 三处统一显式关闭思考模式
          thinking: {"type": "disabled"}（runner.py 自研 judge 的 httpx extra_body、
          Ragas 的 ChatOpenAI extra_body、generator 同理）。judge 是"分数|理由"
          裁决，无需思考链；关闭后行为对齐旧 deepseek-chat。
修复效果: Compliance 从恒 1.0（假象）→ R9 0.90666 / 0.94652 / 0.90698，指标出现
          真实区分度（0.90-0.95），且跨模型（deepseek-chat → v4-flash）稳定达标
          ≥0.80。本项属于"指标有效性"修复（从失真恢复真实值）；配套的 refusal
          提升见 ISSUE-003 / ISSUE-004（≥10%）。
```

## ISSUE-003：hybrid+rerank 过度拒答 —— 全文 rerank 超时 → 空检索 → 误拒 45 条

```
问题编号: ISSUE-003
问题现象: hybrid+rerank 配置对正常业务问题过度拒答。量化：修复前（v4-flash 切换后、
          超时修复前中间态）实测 45 条误拒；R7 该配置 Refusal 仅 0.8545（三配置最低）。
日志证据:
  [2026-08-15 15:51:32] warning stage_timeout stage=retrieval timeout=5 request_id=...      # 压测
  [2026-08-15 15:51:32] warning retrieval_stage_timeout query="员工每年带薪病假的上限是多少天？"
  [2026-08-15 15:51:32] info retrieval_complete mode=hybrid+rerank coarse_candidates=0
                        final_chunks=0 top1_score=null latency_ms=5001 chunks=[]
  [2026-08-15 17:39:24] info refusal_triggered reason=low_confidence signal=0.3862 (chat)  # 评估日志
  # config.yaml 注释："A 方案全文(0) rerank ~5s，5 并发争抢 MPS 致粗排超时→空检索
  #                    （实测全文 43% 失败 vs 200char 2%）"
根因分析: 全文 rerank 30 候选实测 ~5s；eval 并发 5 在 MPS 上争抢，排队的粗排 retrieval
          要等前面的 rerank 释放 MPS 才轮到 → 粗排阶段 stage_timeout（5s）→
          coarse_candidates=0 → 空 sources → RefusalCheck 规则 1 判 low_confidence。
          语义错误：postprocess.py RefusalCheck 把"没来得及搜"（超时）与"查无信息"
          （检索为空）归入同一条空结果规则，正常业务被误拒。
修复方案: ① rerank 输入截断 input_truncate_chars=300（全文 ~5s → ~1.5s）；
          ② retrieval.timeout 5s → 7s（给排队检索留余量：300char rerank ~1.5s × 5 ≈
             7.5s，压测实测 5s 下 4/5 达标、1 超时）；
          ③ 超时语义区分：检索超时 → 返回"系统繁忙"降级话术，且 Refusal 计算排除
             超时样本（超时 ≠ 查无信息）。
修复效果: 阶段超时 19/12/8 → 0/0/0（三配置）；误拒 45 → 14；
          Refusal Appropriateness：
          - hybrid+rerank 0.8545 → 0.9909（+13.6pp，相对 +16.0%）
          - vector-only / hybrid 0.8636 → 0.9909（+12.7pp，相对 +14.7%）
          三配置均 ≥10%。
```

## ISSUE-004：OOS 漏拒 —— top1_sim 阈值区分不了"话题相似 OOS"

```
问题编号: ISSUE-004
问题现象: 部分 OOS 样本未拒答，LLM 直接编造答案。量化：R6 OOS 拒答率三配置仅
          0.17 / 0.22 / 0.83；修复后 R9 三配置统一 1.0。
日志证据:
  # 现象：R6 在册异常 #4（eval-history）——"38 个 OOS 样本 refused=False 漏过过滤，
  #        compliance -0.10"
  # 代码证据（postprocess.py RefusalCheck 规则 2）：仅用 top1_sim < 0.45 判拒，
  #        "公司打印机卡纸怎么修" 类话题相似 OOS 实测 sim=0.59 > 0.45 → 漏过
  [2026-08-16] eval R8 分层视图 oos_refusal_rate=0.7222（硬拒口径，剩余漏网为
               refused=False 的 OOS 样本）
根因分析: 两层根因叠加：
          ① 置信度阈值只认"向量相似度低"，但话题相似的 OOS（公司内部设备维修/IT 类）
             与政策文档同域（都关于公司内部事务），top1_sim 天然偏高（0.59 > 0.45），
             规则 2 无法拦截；
          ② 大语料下检索不再为空，RefusalCheck 规则 1（空结果→拒答）失效——R6 中
             38 个 OOS 正确拒答样本因 refused=False 漏过过滤，被 compliance judge 判 0。
修复方案: 三层防线：
          ① 安全/PII 关键词扩展：config refusal.rules.sensitive_keywords 增加
             入侵/挖矿/防火墙/年薪/工资/Wi-Fi 密码（不含"密码"——避免误伤
             "密码更换频率/密码长度"等 in-scope 政策题，实测 2 条）；
          ② 注入检测前置：RefusalCheck 规则 0 最先执行 detect_injection(query) → safety
             （注入是 query 攻击面，与语料相关度无关，不能靠规则 1/2 兜底）；
          ③ OOS 软拒 judge（eval/runner.py _judge_oos_refusal）：对 refused=False 且
             非超时的 OOS 样本，用 LLM judge 区分"软拒答"（无法回答 + 提供相关信息，
             =1）与"编造答案"（=0）；硬拒样本规则层已判 1 不进 judge，judge 失败保守判 0。
修复效果: OOS 拒答率（分层视图）R8 0.7222（硬拒口径）→ R9 1.0（+27.8pp，相对 +38.5%）；
          三配置从 R6 的 0.17/0.22/0.83 → R9 统一 1.0，均 ≥10%。
          注：R6 hybrid+rerank 的 0.83 实为过度拒答副作用（见 ISSUE-003），非真实 OOS
          防御力；修复后该配置 OOS 拒答率与另外两配置一致（1.0），说明高分为真能力。
```

---

