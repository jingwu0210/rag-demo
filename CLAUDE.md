# CLAUDE.md — 项目级指令（RAG QA Service）

## 铁律：设计承诺不得静默降级

本项目的设计文档（docs/rag-service-design.md）是全流程唯一规格真源。历史上出现过 10 次"设计承诺→实现缺失"问题，其中 4 次是我（Claude）在写任务 brief 时**为了推进度悄悄缩小范围**（静默降级）。以下是强制规则：

1. **禁止静默降级**：设计文档中的任何承诺（指标、格式、阈值、防御），如果实现时想缩小/跳过/降级，**必须先向用户提出**（"X 项成本高，选项 A 降级 / B 实现 / C 推迟"），得到明确同意后才能改。自己拍板跳过 = 违规。
2. **承诺-验收硬链接**：设计文档每个承诺条目必须标注"由哪个 Task/Phase 的哪条验收标准落地"。写任何计划或 brief 前，逐条对照设计文档承诺执行反向追溯扫描（检查清单见 .superpowers/sdd 台账的"反向追溯扫描"节）。
3. **brief 是转述，转述会失真**：SDD 的 task review 只检查"实现 vs brief"，不检查"brief vs 设计文档"。因此写 brief 的接口/范围部分时，必须 grep 设计文档原文核对，不允许凭记忆转述。
4. **配置项必须有消费者**：config.yaml 每新增一个开关/键，同任务内必须有读取它的代码 + 触发它的测试。config 声称 true 但代码无消费者的"配置孤儿"是缺陷。
5. **"逐字使用"指令的陷阱**：brief 标记"逐字使用"的代码块必须完整覆盖该能力的全部分支（格式、异常、边界），只给主路径的逐字代码会把设计意图锁死在残缺实现里。
6. **设计假设必须实测**：设计文档中的类型/阈值/协议假设（如"chromadb where 支持字符串比较"）在实现任务内必须有一个真实环境的验证步骤，不验证就写进代码 = 埋雷。
7. **防御承诺必须有失败测试**：brief 出现"X 失败不中断/降级"类承诺时，必须同时要求一个真实触发 X 失败的测试。
8. **修改必须全局一致**：修改计划/文档/代码的某个决策时，必须 grep 全文找出同主题的所有引用点（根因表、实施步骤、配置清单、设计文档），一处决策改动全部同步。点更新后声称"已更新"是糊弄 — 2026-08-13 的 plan 曾出现"职责分治方案已更新但根因表仍写旧方案"的三处不一致，被用户追问才暴露。
9. **计划不得压缩已确认的详细设计**：讨论中与用户确认过的逐字段设计（如日志 schema、prompt 原文、聚合公式）必须完整落入计划文件，压缩成一行摘要 = 偷懒。计划是实现的唯一依据 — 2026-08-13 曾把 8 事件日志 schema 压缩成表格一行，被用户追问两次才补全。
10. **数据文件清理三重防线**（2026-08-13 事故：smoke 前 `rm -f data/cache.db` 把评估写好的 eval_history + per_qa 明细全部误删，before/after 对比依据丢失）：
    - **cache.db 是混合库**：cache_entries（缓存，可清）+ eval_history（评估历史，不可清）+ request_metrics（运营指标）+ turns/sessions（对话记录）— 清缓存 ≠ 删库
    - 清缓存必须走应用层 `CacheManager.invalidate_all()`，禁止 rm 数据库文件
    - 任何 smoke/测试要动数据时必须用临时 DB：`ConfigRegistry.override("paths.sqlite", tmp_path)`，禁止操作生产库
    - 删除数据文件前先备份（`cp data/cache.db /tmp/backup-<ts>.db`）
11. **批量任务设计时必须先做耗时估算**（2026-08-13：评估脚本串行设计，65 条×3 配置≈900 次 LLM 调用跑了 40 分钟/轮 × 4 轮，用户质疑后才优化并发。系统本身支持 5 并发（assignment 要求），评估却按 1 并发跑）：写任何批量/循环类任务（评估、ingest、测试）前，先算"单次耗时 × 次数 / 并发度"，超过 5 分钟的设计必须包含并发方案，不得"先跑通再优化"。
12. **未经明确指示不得 git commit**（2026-08-15 用户指令）：除非用户明确要求提交，否则所有改动留在工作区（未提交状态）。完成一批工作后报告改动内容，由用户决定何时提交、如何拆分 commit。

## 环境事实（避免重复踩坑）

- Python 3.9.6，系统无全局 python → 统一用 `.venv/bin/python` / `.venv/bin/pytest`
- pip 必须走清华镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple`
- HuggingFace 直连不可用 → 模型下载用 `HF_ENDPOINT=https://hf-mirror.com`（偶发 SSL 抖动，重试自愈）
- DeepSeek API key 存环境变量 `DEEPSEEK_API_KEY`，绝不写入 config/代码/仓库
- chromadb 0.5.23：where 仅支持 metadata 字段寻址；多条件必须 `{"$and": [...]}`；`$gte` 仅支持 int/float（日期存整数 YYYYMMDD）
- structlog 24.4.0：`add_logger_name` 与 PrintLoggerFactory 不兼容，日志无 logger name 字段
- asyncio run_in_executor 静默丢弃 kwargs → 提交线程池的函数必须传位置参数
- 重模型（BGE-M3 / bge-reranker）首次加载 >2s，阶段超时保护必须把模型预热排除在外
- 检索分数语义：vector=余弦(0-1)、hybrid=RRF(~0.03)、rerank=CrossEncoder(无界) — 任何阈值比较必须按 mode 区分

## 交付物清单（assignment 硬要求）

- [x] 完整代码 + config.yaml（100+ 配置项）
- [x] 一键评估脚本 eval.sh
- [ ] 评估报告（三配置对比 + before/after + ≥2 个 Issue Diagnosis 案例，改进 ≥10%）
- [x] 日志字段字典 docs/log-field-dictionary.md + 样本日志
- [ ] 设计文档版本记录维护（§0，每次修改必须更新）

## 沟通约定

- 文档和注释用中文，代码/commit message 用英文
- Bug 修复流程：先 Root Cause + Proposed Fix Plan 给用户确认，再动手
