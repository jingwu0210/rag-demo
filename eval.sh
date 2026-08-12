#!/bin/bash
set -e
cd "$(dirname "$0")"
.venv/bin/python -m eval.runner --output data/eval/results/ 2>&1 | tee data/logs/eval-run.log
echo "评估完成: data/eval/results/report.csv"
