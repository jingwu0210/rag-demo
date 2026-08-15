#!/bin/bash
set -e
cd "$(dirname "$0")"

# ── 前置检查：模型 API key（唯一外部依赖，其余全部自举）──────────
if [ -z "${DEEPSEEK_API_KEY}" ]; then
    echo "错误: 缺少 DEEPSEEK_API_KEY 环境变量（服务 LLM 生成需要）。" >&2
    echo "用法: export DEEPSEEK_API_KEY=<your-key> && ./run.sh" >&2
    exit 1
fi

# ── 镜像兜底（国内直连不可用；已设环境变量则尊重用户配置）────────
# HF_ENDPOINT: HuggingFace 模型下载镜像；PIP_INDEX_URL: pip 镜像
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"

if [ ! -d .venv ]; then
    echo "创建虚拟环境..."
    python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt -i "$PIP_INDEX_URL"
mkdir -p data/{corpus,chroma,ocr,logs,eval/results}

# ── 幂等引导：知识库为空时自动生成语料并入库 ─────────────────────
# 老用户重启服务不会重复入库；NO_BOOTSTRAP=1 可强制跳过
if [ "${NO_BOOTSTRAP}" != "1" ]; then
    ACTIVE_COUNT=$(.venv/bin/python -c "
from core.config import ConfigRegistry
from storage.chroma_client import ChromaStore
ConfigRegistry.init('config.yaml')
s = ChromaStore()
print(len(s.collection.get(where={'is_active': True})['ids']))
" 2>/dev/null || echo "0")
    if [ "${ACTIVE_COUNT:-0}" = "0" ]; then
        echo "知识库为空，开始引导（首次需下载 BGE-M3 模型，可能需要几分钟）..."
        .venv/bin/python scripts/generate_corpus.py
        .venv/bin/python scripts/ingest_corpus.py
        .venv/bin/python scripts/verify_corpus.py
    else
        echo "知识库已有 ${ACTIVE_COUNT} 个 active chunks，跳过引导"
    fi
fi

echo "启动 RAG QA Service: http://localhost:8000"
echo "API 文档: http://localhost:8000/docs"
# exec 继承上述 export（uvicorn startup 预热 BGE-Reranker 时经镜像下载模型）
exec .venv/bin/uvicorn api.app:app --host 0.0.0.0 --port 8000
