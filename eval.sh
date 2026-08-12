#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p data/logs
.venv/bin/python -m eval.runner --output data/eval/results/ 2>&1 | tee data/logs/eval-run.log
echo "评估完成: data/eval/results/eval_report.csv"
