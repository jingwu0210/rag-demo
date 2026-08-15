# 演示 UI 设计文档（/rag-gen-ai-service-demo）

> 本文档描述演示工作台（`api/static/rag-gen-ai-service-demo.html`）的设计，与实现一一对应。
> 规格真源为本文档；实现变更须同步本文档（铁律 8：修改必须全局一致）。

---

## 1. 定位

| 维度 | 说明 |
|------|------|
| 形态 | 无框架纯静态单文件（HTML + CSS + JS 全部内嵌），无构建步骤、无外部依赖 |
| 路径 | GET `/rag-gen-ai-service-demo` 返回静态页；页面内全部请求为同源相对路径 |
| 目的 | 将服务 7 个业务 API（/chat、/ingest、/eval/run、/eval/result、/report、/health、/config）全部可视化演示，替代 curl 交互 |
| 场景 | 单实例本地演示；无鉴权、无路由、无持久化服务端状态（会话/模式偏好存浏览器 localStorage） |

页面为单页应用式视图切换：sidebar 按钮控制 section 显隐（`.page.active`），无 URL 路由。

## 2. 设计系统

设计语言沿用参考实现——`demo.html` 注释明确"沿用 intra-rag demo.html 视觉语言"，本文档将其固化为以下 token 体系：

**色彩 token（CSS 变量集中定义）**

| Token | 值 | 用途 |
|-------|-----|------|
| `--bg` | `#f4f6f6` | 页面浅色背景 |
| `--surface` | `#ffffff` | 面板卡片表面 |
| `--ink` / `--mut` | `#0c1420` / `#5b6774` | 正文 / 弱化文字 |
| `--line` | `#dfe5e8` | 边框 |
| `--teal` / `--teal-2` | `#0e7490` / `#0891b2` | 主色 / hover 变体 |
| `--teal-soft` | `#e0f2f7` | 主色浅底（选中态、聊天气泡） |
| `--ok` / `--amber` / `--bad` | `#059669` / `#f59e0b` / `#dc2626` | 语义状态色，各带 `-soft` 浅底 |

**组件语言**

- 面板卡片：圆角 14px + 1px 边框；panel-head 内含 mono 大写眉标签（`.k`）+ 描述 + 右侧计数；可折叠面板带 caret 旋转
- 等宽字体：`--mono`（ui-monospace 栈）用于标签、数值、日志、配置树、代码
- 控件：segmented control（检索模式 / 子 tab 切换）、tag / pill / chip、banner（ok/warn/bad 三态）、score-badge（0 分红、高分绿、中间琥珀、无评分灰）、toast 右上角通知
- 全局细节：`:focus-visible` teal 焦点环；表头 mono 大写；响应式断点 1080px（侧栏横排、会话列降级）与 720px（单列）；`prefers-reduced-motion` 关闭动画

## 3. 信息架构

Sidebar 六个视图（JS 切换，无路由）。头部全局状态徽章独立于视图：每 15s 轮询 `/health`，三态显示（ok=服务正常 / degraded=服务降级 / off=服务离线，悬停展示 components 与并发占用）。

| 视图 | 对应 API | 关键交互 |
|------|---------|---------|
| 💬 Chat | POST /chat | 请求级检索模式切换、多轮会话（localStorage）、答案/来源/prompt 展示 |
| 📥 Ingest | POST /ingest | 拖拽上传 + doc_type/version 参数、入库结果卡片 |
| 🚀📊 Eval | POST /eval/run、GET /eval/result | 触发（202 + run_id）、六指标对比 + 分层视图 + per_qa 明细 |
| 📈 Report | GET /report | CSV 前端解析、KPI 卡片 + 全量指标表 |
| 🔧 Maintenance | GET /health、GET /logs、GET /db/tables、GET /db/table/{name} | 三子 tab：Health / Logs / Database |
| ⚙️ Config | GET /config | config.yaml 只读树，分组折叠 |

## 4. 各页设计

### 4.1 💬 Chat（POST /chat）

布局为左会话列（固定 350px、sticky）+ 右 workspace 三面板。

- **会话**：session_id 持久化 localStorage（`ragdemo.session_id`），多轮追问自动携带；"清空"按钮删除会话并重置各面板；5 个快捷问题 chips（覆盖中英、术语、OOS 场景）
- **检索模式**：请求级 segmented control（vector-only / hybrid / hybrid+rerank），"对下一轮对话生效"；选择持久化 localStorage（`ragdemo.mode`），默认 hybrid+rerank——与评估三配置对齐
- **答案面板**：banner 区分"已拒答"（红，附拒答原因）/ "已回答"（绿，缓存命中与超时降级追加说明）；答案正文去除内联引用标记（`[chunk_id: …]` 与 `文件名#段#序号`），渲染 `**粗体**` 与 `` `代码` ``
- **元信息条**：pill 展示 mode（响应中的实际生效值）、final_chunks（AdaptiveK）、本次延迟、token、session；缓存命中 / partial 超时降级追加 warn pill
- **检索来源面板**：AdaptiveK 动态截断后的最终 chunks 卡片——序号、heading_path/source、chunk_id、score（相对最大分数 70% 以上绿色高亮）、正文查询词高亮（取首个命中）
- **Prompt 面板**：响应含 prompt 字段时显示，可折叠；渲染兼容纯字符串与 `{messages: [{role, content}]}` 结构化两种形态（暗色代码块、role 着色）；无字段时整区隐藏（见 §6）

### 4.2 📥 Ingest（POST /ingest）

- **dropzone**：拖拽/点击选择文件（pdf / docx / md / txt），选中后显示文件名与大小
- **参数**：doc_type 下拉（handbook / compliance / technical / architecture）+ version 文本框（默认 v1.0，hint "同文档新版本将替换旧 chunk"）
- **结果卡片**：status 着色（ingested 绿「已入库」/ replaced 琥珀「已替换旧版本」/ skipped 灰「已跳过」）+ 字段网格 chunks_created / chunks_replaced / doc_hash / source_file / version，reason 有则追加显示

### 4.3 🚀📊 Eval（POST /eval/run + GET /eval/result）

单页上下两个 section 合并（触发 + 结果查询）。

**上 section — 评估触发**

- 只读信息卡四张：测试集路径（叠加最近一次评估条数）、对比配置 chips、并发度（"问答 + judge"）、最近一次 run_id——数据源为 GET /config 与 GET /eval/result（无记录不阻塞页面）
- "🚀 触发评估" → POST /eval/run 返回 202 + run_id，展示 run_id 并附带"查看结果"按钮直接跳转查询

**下 section — 评估结果**

- 查询行：run_id 输入框（留空 = 最近一次）+ "加载 per_qa 明细" checkbox（默认勾选，映射 detail=true）+ 查询按钮（Enter 亦可触发）
- 渲染顺序：
  1. 摘要条：run_id / 时间 / 测试集条数 / test_set_hash 前 8 位
  2. 六指标对比表：行 = 指标（faithfulness、context_precision、context_recall、answer_relevancy、answer_compliance、refusal_appropriateness），列 = 配置；数值 0-1 按百分比格式化，缺失显示 "—"
  3. 分层视图：生成质量 / 检索质量 / 合规行为三组（层行 + 指标行），加性能层（P50/P95 延迟、平均 token/请求）
  4. per_qa 明细：按 config 分组折叠，组头统计"条数 · 拒答 n · 超时 n"
- **per_qa 条目**：judge 评分徽章（0 分红 / ≥4.5 绿 / 中间琥珀 / 无评分灰，悬停显示 judge_reason）+ 状态标记（⛔拒答 / ⏱超时 / 💾缓存）+ faith/prec 摘要；展开显示 9 行明细：问题、答案（可滚动）、judge 理由、faithfulness、context_precision、refusal_appropriateness、拒答（含原因）、延迟与 token、来源数与 OOS

### 4.4 📈 Report（GET /report）

- 刷新按钮触发 GET /report（text/csv），前端解析首行表头 `metric,value` 的键值对
- **KPI 卡片 8 张**：P50 延迟、P95 延迟、总 token、平均 token/请求、缓存命中率、拒答率、PII 脱敏总数、注入拦截总数（比率指标 ×100 保留 1 位小数）
- 全量 metric/value 表 + 顶部计数"共 N 条请求"；无数据（仅表头）→ 空态提示"先去 Chat 页发起几轮对话后回来刷新"

### 4.5 🔧 Maintenance（GET /health + /logs + /db/*）

**🩺 Health 子 tab**

- 整体状态 banner（ok 绿 / degraded 琥珀）+ 启动异常 banner（startup_error 有则显示，见 §6）
- 组件卡片：chromadb 🗄️ / llm 🤖 / sqlite 💾，ok/error 着色（图标映射未知组件回退 ⚙️）
- 并发占用：active/max tag + 说明"达到上限时新请求返回 429"
- 手动刷新按钮（头部徽章独立于本 tab，固定 15s 轮询）

**📜 Logs 子 tab**

- 日志文件下拉（*.log，排除 *.stderr.log，按修改时间倒序）+ 行数选择（50 / 200 / 1000 / 5000）+ 加载按钮
- 深色终端风 tail 块（等宽字体、绿色文字）；响应兼容多种形态（见 §6）

**🗄️ Database 子 tab**

- 表清单：六张业务表按钮（表名 + 行数），点击加载预览
- 预览卡片：列名表头 + 行数据（最多显示 100 行，超长单元格省略并悬停显示全文）+ "← 返回表清单"按钮

### 4.6 ⚙️ Config（GET /config）

- 只读配置树：顶层键（retrieval / eval / paths / llm 等）分组折叠面板
- 组内递归渲染：嵌套 dict 缩进分层（最多 3 级）、数组 of 对象按 name（或首键）展开、字符串 teal 着色 / 数字琥珀着色、标量直接展示
- "🔄 重新加载"按钮重新拉取

## 5. API 契约

以下为演示 UI 直接消费的接口形态（与 routes.py 实现一致）。

### 5.1 POST /chat（扩展：+mode 请求级参数）

- 请求 JSON：`{query: str(必填), session_id?: str, mode?: "vector-only"|"hybrid"|"hybrid+rerank"}`
- mode 可选；缺省 → 用 config 全局 `retrieval.mode`；**非法值 → 422** `{detail: "未知检索模式：…"}`
- 响应 200：`{answer, session_id, mode(请求级覆盖生效后的实际值), sources[], timing_ms{}, token_usage{}, refused, refusal_reason?, from_cache, partial}`
- 并发达上限 → 429 `{detail: "系统繁忙，请稍后重试"}` + 头 `Retry-After: 1`

### 5.2 POST /ingest

- 请求：multipart/form-data `file` + `doc_type` + `version`（缺省 v1.0）
- 响应：`{status: ingested|skipped|replaced, chunks_created, chunks_replaced, doc_hash, source_file, version, reason}`
- 入库异常 → 500 `{detail}`

### 5.3 POST /eval/run

- 请求无 body；响应 202：`{run_id: "eval_YYYYMMDD_HHMMSS_xxxxxx"}`（后台线程执行三配置对比）
- chat_service 未初始化 → 503

### 5.4 GET /eval/result（扩展：+detail=true）

- 参数：`run_id`（缺省 = 最近一次 run）、`detail`（true → 追加 per_qa）
- 响应：`{run_id, results: [指标行按 config_name 排序], per_qa?: {config_name: [per_qa 条目]}}`
- results 行字段：run_id、config_name、timestamp、test_set_hash、total_qa_pairs、faithfulness、context_precision、context_recall、answer_relevancy、answer_compliance、style_consistency、refusal_appropriateness、p50_latency_ms、p95_latency_ms、avg_tokens_per_call、total_pii_redactions、total_injections_blocked
- per_qa 条目字段：question、answer、refused、refusal_reason、from_cache、timeout、faithfulness、context_precision、refusal_appropriateness、answer_compliance、judge_reason（"分数|理由"）、latency_ms、tokens_total、sources_count、is_out_of_scope 等（不含 chunks_text）
- 无记录：`{run_id: null, results: []}`

### 5.5 GET /logs

- 无参：`{files: [*.log 文件名，排除 *.stderr.log，按 mtime 倒序], default: 最新文件}`
- `?file=<name>&lines=N`：`{file, lines, content}`；N clamp 到 [1, 1000]（前端下拉含 5000 选项，后端上限 1000）
- 文件名含 `/` `\` `..`、以 `.` 开头或非 `.log` 结尾，或文件不存在 → 404

### 5.6 GET /db/tables

- 响应：`{tables: [{name, rows}]}`——白名单六表（cache_entries / eval_history / request_metrics / turns / sessions / ingest_log），表缺失视为 rows=0

### 5.7 GET /db/table/{name}

- 参数：`limit`（缺省 50，clamp [1, 200]）
- 响应：`{name, columns: [列名], rows: [值数组行]}`（按列顺序对齐；字符串字段超 500 字符截断）
- 非白名单表名 → 404

### 5.8 GET /config

- 响应：config.yaml 解析后的 dict（含 RAG_ 环境变量覆盖），JSON

### 5.9 GET /rag-gen-ai-service-demo

- 响应：text/html 静态页（FileResponse）；文件缺失 → 404

### 5.10 GET /health（演示 UI 消费）

- 响应：`{status: ok|degraded, components: {chromadb, sqlite, llm: ok|error}, concurrency: {active, max}}`
- 头部徽章 15s 轮询；Maintenance>Health 手动刷新

## 6. 降级策略（前端对字段缺失的兜底）

当前响应契约与前端渲染能力之间存在差距，前端一律向前兼容（后端扩展字段后无需改动即自动生效）：

| 场景 | 兜底行为 |
|------|---------|
| 响应无 `final_chunks` 字段（当前契约未含） | 回退 `sources.length`，仍标注"（AdaptiveK）" |
| 响应无 `prompt` 字段（当前契约未含） | Prompt 面板整体隐藏；渲染兼容字符串 / `{messages}` 两种形态 |
| `/health` 无 `startup_error` 字段（当前契约未含） | 启动异常 banner 不显示，不报错 |
| per_qa 条目无 `judge_score` 键（judge 输出为"分数\|理由"合并于 judge_reason） | 优先读 judge_score；缺失时正则提取 judge_reason 首位数字；再缺失 → "无评分"灰徽章 |
| per_qa 为空 / detail 未生效 | 显示降级态文案"该运行无 per_qa 明细（detail 参数未生效或接口未实现）" |
| 指标值为 null / 空串 | 显示 "—" + no-data 弱化样式，表格不崩坏 |
| /logs 响应形态差异 | files 兼容 `{files}` / 数组 / `{log_files}`；tail 兼容 content / tail / log 字段（字符串或对象），未知形态 JSON 兜底 |
| /db/tables 响应形态差异 | 兼容 `{tables}` 数组或裸数组；表名与行数分别兼容 `name/table`、`rows/row_count` 键 |
| 请求失败（非 2xx） | 统一 apiJSON/apiText 封装：提取后端 detail 抛错 → 页面区块显示错误空态 + toast |

## 7. 安全边界

- **无鉴权定位**：与 /report 同级——单实例本地演示服务；演示 UI 不引入登录层，接口全量返回（/config 含完整配置 dict）
- **数据库浏览隔离**：/db/* 仅白名单六张业务表（杜绝任意表名注入），非白名单 404；行数 limit ≤ 200、字符串字段 500 字符截断（响应体积与渲染保护）
- **日志文件名过滤**：拒绝 `/`、`\`、`..`、隐藏文件、非 `.log` 结尾 → 404（防路径穿越）；行数 clamp [1, 1000]
- **上传文件名清洗**：后端 `os.path.basename` 归一化；前端文件名展示经转义
- **前端 XSS 防御**：动态文本一律 textContent 或 esc() 转义（答案、chunk 文本、日志、配置值、per_qa 明细）；HTML 注入点仅限受控渲染函数（`**粗体**`/`` `代码` `` 标记与查询词高亮）
