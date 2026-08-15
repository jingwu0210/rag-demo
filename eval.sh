#!/bin/bash
set -e
cd "$(dirname "$0")"

# ══════════════════════════════════════════════════════════
# eval.sh — 一键评估（与 run.sh 同一交付契约与输出风格）
# 前置检查 → 三配置评估（进度条）→ 结果摘要打印到终端
# ══════════════════════════════════════════════════════════

# ── ① 前置检查 ──────────────────────────────────────────
if [ -z "${DEEPSEEK_API_KEY}" ]; then
    echo "❌ 缺少 DEEPSEEK_API_KEY 环境变量（评估需要 DeepSeek 跑生成与 judge）。" >&2
    echo "   用法: export DEEPSEEK_API_KEY=<your-key> && ./eval.sh" >&2
    exit 1
fi
echo "🔑 API key: 已配置 ✓"

if [ ! -d .venv ]; then
    echo "❌ 未检测到 .venv，请先运行 ./run.sh（自动创建环境 + 下载依赖 + 引导知识库）。" >&2
    exit 1
fi

ACTIVE_COUNT=$(.venv/bin/python -c "
from core.config import ConfigRegistry
from storage.chroma_client import ChromaStore
ConfigRegistry.init('config.yaml')
s = ChromaStore()
print(len(s.collection.get(where={'is_active': True})['ids']))
" 2>/dev/null || echo "0")
if [ "${ACTIVE_COUNT:-0}" = "0" ]; then
    echo "❌ 知识库为空，评估结果将全部拒答。请先运行 ./run.sh 完成引导（生成语料 + 入库）。" >&2
    exit 1
fi

# 模型检查（同 run.sh：HF 缓存存在性；评估需要 BGE-M3 编码 + reranker 精排）
model_cached() {
    local cache_dir="${HF_HOME:-$HOME/.cache/huggingface}/hub/models--${1//\//--}"
    [ -d "$cache_dir/snapshots" ] || return 1
    local f
    for f in model.safetensors pytorch_model.bin onnx/model.onnx; do
        if ls "$cache_dir"/snapshots/*/"$f" >/dev/null 2>&1; then
            return 0
        fi
    done
    return 1
}
for m in BAAI/bge-m3 BAAI/bge-reranker-v2-m3; do
    if model_cached "$m"; then
        echo "🤖 $m: 已缓存 ✓"
    else
        echo "❌ $m 未缓存 — 请先运行 ./run.sh（自动下载模型）。" >&2
        exit 1
    fi
done

# ── 镜像兜底（同 run.sh：HF 镜像 + 代理兼容）──────────────
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}pypi.tuna.tsinghua.edu.cn,hf-mirror.com"
export no_proxy="$NO_PROXY"

mkdir -p workspace/logs
# 每次评估独立日志文件（带时间戳），不覆盖历史评估的日志证据
LOG_FILE="workspace/logs/eval-$(date +%Y%m%d_%H%M%S).log"

TEST_SET=$(.venv/bin/python -c "
from core.config import ConfigRegistry
ConfigRegistry.init('config.yaml')
import json
with open(ConfigRegistry.get('eval.test_set_path')) as f:
    print(len(json.load(f)))
" 2>/dev/null || echo "?")
CONFIGS=$(.venv/bin/python -c "
from core.config import ConfigRegistry
ConfigRegistry.init('config.yaml')
print(len(ConfigRegistry.get('eval.compare_configs', [])))
" 2>/dev/null || echo "3")

# ── ② 评估（后台跑 + 进度条）────────────────────────────
echo "🚀 开始评估"
echo "   测试集: ${TEST_SET} 条 × ${CONFIGS} 个检索配置（vector-only / hybrid / hybrid+rerank）"
echo "   并发: 5（config eval.concurrency），预计 40-50 分钟（A 方案全文 rerank 后）"
# stdout = structlog JSON 结构化日志（符合日志字段字典）；
# stderr = 第三方库裸噪音，单独存 .stderr.log，不污染主日志文件
.venv/bin/python -m eval.runner --output workspace/results/ \
    > "$LOG_FILE" 2> "${LOG_FILE%.log}.stderr.log" &
RUNNER_PID=$!

# 进度条：轮询日志里的 eval_progress 事件（done/total/config）
TOTAL_QA=$((TEST_SET * CONFIGS))
DONE=0
while kill -0 "$RUNNER_PID" 2>/dev/null; do
    PROG=$(.venv/bin/python -c "
import json, sys
try:
    with open('$LOG_FILE') as f:
        lines = f.readlines()
    done = total = 0
    cfg = ''
    for l in lines:
        try:
            d = json.loads(l)
        except Exception:
            continue
        if d.get('event') == 'eval_progress':
            done = d.get('done', done)
            total = d.get('total', total)
            cfg = d.get('config', cfg)
    # 累加已完成配置的 QA 数（每配置 done 从 0 起，需按 config 累计——简化：显示当前配置进度）
    print(f'{done} {total} {cfg}', end='')
except Exception:
    print('0 0', end='')
")
    read -r DONE TOTAL CFG <<< "$PROG"
    if [ "${TOTAL:-0}" -gt 0 ] 2>/dev/null; then
        PCT=$((DONE * 100 / TOTAL))
        BAR_LEN=30
        FILLED=$((PCT * BAR_LEN / 100))
        BAR=$(printf '%*s' "$FILLED" '' | tr ' ' '█')
        EMPTY=$(printf '%*s' "$((BAR_LEN - FILLED))" '' | tr ' ' '░')
        printf "\r  [%s%s] %3d%%  %s 配置 %s/%s  " "$BAR" "$EMPTY" "$PCT" "$CFG" "$DONE" "$TOTAL"
    fi
    sleep 1
done
printf "\n"
wait "$RUNNER_PID"

# ── ③ 结果摘要（终端直接看三配置关键指标）──────────────────
echo ""
echo "✅ 评估完成"
echo "════════════════ 结果摘要 ════════════════"
.venv/bin/python -c "
import csv
with open('workspace/results/eval_report.csv') as f:
    rows = list(csv.DictReader(f))
def g(r, k, w=7):
    v = r.get(k, '')
    try:
        return f'{float(v):>{w}.4f}'
    except (TypeError, ValueError):
        return f'{v:>{w}}'
print(f\"{'Config':<15} {'Faith':>7} {'CP':>7} {'Compl':>7} {'Refusal':>7} {'Style':>7} {'P95ms':>7}\")
for r in rows:
    print(f\"{r['config']:<15} {g(r,'faithfulness')} {g(r,'context_precision')} {g(r,'answer_compliance')} {g(r,'refusal_appropriateness')} {g(r,'style_consistency')} {g(r,'p95_ms',8)}\")
" 2>/dev/null || echo "（摘要读取失败，请查看报表文件）"
echo "════════════════════════════════════════════"
echo "   完整报表: workspace/results/eval_report.csv（Markdown: eval_report.md）"
echo "   评估日志: $LOG_FILE"
echo "   噪音日志: ${LOG_FILE%.log}.stderr.log"
