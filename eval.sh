#!/bin/bash
set -e
cd "$(dirname "$0")"

# ── 前置检查：模型 API key（唯一外部依赖，其余全部自举）──────────
if [ -z "${DEEPSEEK_API_KEY}" ]; then
    echo "错误: 缺少 DEEPSEEK_API_KEY 环境变量（评估需要 DeepSeek 跑生成与 judge）。" >&2
    echo "用法: export DEEPSEEK_API_KEY=<your-key> && ./eval.sh" >&2
    exit 1
fi

# ── 环境自举检查：venv 与知识库由 run.sh 引导 ──────────────────
if [ ! -d .venv ]; then
    echo "未检测到 .venv，请先运行 ./run.sh（自动创建环境 + 下载依赖 + 引导知识库）。" >&2
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
    echo "知识库为空，评估结果将全部拒答。请先运行 ./run.sh 完成引导（生成语料 + 入库）。" >&2
    exit 1
fi

# ── HuggingFace 镜像兜底（国内直连不可用；已设 HF_ENDPOINT 则尊重用户配置）──
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

# ── 代理兼容：镜像域名绕过系统代理直连（同 run.sh）──────────
# 不可达的系统代理会让模型下载报 ProxyError；镜像直连即可达，DeepSeek API
# 仍走用户代理（海外环境依赖代理时不受影响）。
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}pypi.tuna.tsinghua.edu.cn,hf-mirror.com"
export no_proxy="$NO_PROXY"

mkdir -p workspace/logs
# 每次评估独立日志文件（带时间戳），不覆盖历史评估的日志证据
LOG_FILE="workspace/logs/eval-$(date +%Y%m%d_%H%M%S).log"
# stdout = structlog JSON 结构化日志（符合日志字段字典）；
# stderr = 第三方库裸噪音（urllib3 警告/chromadb telemetry/tokenizers），
# 单独存 .stderr.log，不污染主日志文件
.venv/bin/python -m eval.runner --output workspace/results/ \
    > "$LOG_FILE" 2> "${LOG_FILE%.log}.stderr.log"
echo "评估完成: workspace/results/eval_report.csv"
echo "评估日志: $LOG_FILE"
echo "噪音日志: ${LOG_FILE%.log}.stderr.log"
