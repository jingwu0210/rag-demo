#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p data/logs
# 每次评估独立日志文件（带时间戳），不覆盖历史评估的日志证据
LOG_FILE="data/logs/eval-$(date +%Y%m%d_%H%M%S).log"
.venv/bin/python -m eval.runner --output data/eval/results/ 2>&1 | tee "$LOG_FILE"
echo "评估完成: data/eval/results/eval_report.csv"
echo "评估日志: $LOG_FILE"
