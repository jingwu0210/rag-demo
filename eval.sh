#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p data/logs
# 每次评估独立日志文件（带时间戳），不覆盖历史评估的日志证据
LOG_FILE="data/logs/eval-$(date +%Y%m%d_%H%M%S).log"
# stdout = structlog JSON 结构化日志（符合日志字段字典）；
# stderr = 第三方库裸噪音（urllib3 警告/chromadb telemetry/tokenizers），
# 单独存 .stderr.log，不污染主日志文件
.venv/bin/python -m eval.runner --output data/eval/results/ \
    > "$LOG_FILE" 2> "${LOG_FILE%.log}.stderr.log"
echo "评估完成: data/eval/results/eval_report.csv"
echo "评估日志: $LOG_FILE"
echo "噪音日志: ${LOG_FILE%.log}.stderr.log"
