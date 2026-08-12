#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
    echo "创建虚拟环境..."
    python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
mkdir -p data/{corpus,chroma,ocr,logs,eval/results}
echo "启动 RAG QA Service: http://localhost:8000"
echo "API 文档: http://localhost:8000/docs"
exec .venv/bin/uvicorn api.app:app --host 0.0.0.0 --port 8000
