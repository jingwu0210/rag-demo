#!/bin/bash
set -e
cd "$(dirname "$0")"

# ══════════════════════════════════════════════════════════
# run.sh — 一键启动（唯一外部依赖：DeepSeek API Key）
# 流程：前置检查（key/依赖/模型）→ 语料管理（交互）→ 启动服务
# NON_INTERACTIVE=1 跳过全部交互询问（CI/自动化场景）
# ══════════════════════════════════════════════════════════

# ── ① 前置检查 ──────────────────────────────────────────
if [ -z "${DEEPSEEK_API_KEY}" ]; then
    echo "❌ 缺少 DEEPSEEK_API_KEY 环境变量（服务 LLM 生成需要）。" >&2
    echo "   用法: export DEEPSEEK_API_KEY=<your-key> && ./run.sh" >&2
    exit 1
fi
echo "🔑 API key: 已配置 ✓"

# 镜像兜底（国内直连不可用；已设环境变量则尊重用户配置）
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
# 代理兼容：镜像域名绕过系统代理直连；DeepSeek API 仍走用户代理
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}pypi.tuna.tsinghua.edu.cn,hf-mirror.com"
export no_proxy="$NO_PROXY"

if [ ! -d .venv ]; then
    echo "📦 创建虚拟环境..."
    python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt -i "$PIP_INDEX_URL"
echo "📦 依赖: 已就绪 ✓"

# 模型检查（HF 缓存 + 关键权重文件存在性；未缓存则先下载）
# 逐个候选文件名检查：ls 多 glob 时前一个不匹配会带出非零退出码导致误判
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
MODELS="BAAI/bge-m3 BAAI/bge-reranker-v2-m3"
MISSING=""
for m in $MODELS; do
    if model_cached "$m"; then
        echo "🤖 $m: 已缓存 ✓"
    else
        echo "🤖 $m: 未缓存（约 2.2GB）"
        MISSING="$MISSING $m"
    fi
done
if [ -n "$MISSING" ]; then
    echo "⏳ 开始下载模型（首次约需数分钟，经 HF_ENDPOINT 镜像）..."
    for m in $MISSING; do
        echo "  ⏳ 下载 $m ..."
        .venv/bin/python -c "
from huggingface_hub import snapshot_download
snapshot_download('$m')
" 2>/dev/null || { echo "❌ $m 下载失败（网络问题？可重试 ./run.sh）" >&2; exit 1; }
    done
    echo "🤖 模型下载完成 ✓"
fi

mkdir -p assets/{corpus,testsets,chroma} workspace/{ocr,logs,results}

# ── ② 语料管理 ──────────────────────────────────────────
ACTIVE_COUNT=$(.venv/bin/python -c "
from core.config import ConfigRegistry
from storage.chroma_client import ChromaStore
ConfigRegistry.init('config.yaml')
s = ChromaStore()
print(len(s.collection.get(where={'is_active': True})['ids']))
" 2>/dev/null || echo "0")

if [ "${ACTIVE_COUNT:-0}" = "0" ]; then
    # 空库自动引导（无交互语义）：用户语料优先，否则生成演示语料
    USER_DOCS=$(find assets/corpus -type f \( -name "*.pdf" -o -name "*.docx" \
        -o -name "*.md" -o -name "*.txt" \) 2>/dev/null | head -1)
    if [ -n "${USER_DOCS}" ]; then
        echo "🗂️  知识库为空，检测到 assets/corpus/ 已有语料，直接入库..."
        .venv/bin/python scripts/ingest_corpus.py 2>/dev/null | grep -vE "ppocr DEBUG|Namespace" || true
    else
        echo "🗂️  知识库为空，生成演示语料并入库..."
        .venv/bin/python scripts/generate_corpus.py 2>/dev/null
        .venv/bin/python scripts/ingest_corpus.py 2>/dev/null | grep -vE "ppocr DEBUG|Namespace" || true
        HF_HUB_OFFLINE=1 .venv/bin/python scripts/verify_corpus.py 2>/dev/null | grep -E "^  \[|全部通过|失败" || true
    fi
    ACTIVE_COUNT=$(.venv/bin/python -c "
from core.config import ConfigRegistry
from storage.chroma_client import ChromaStore
ConfigRegistry.init('config.yaml')
s = ChromaStore()
print(len(s.collection.get(where={'is_active': True})['ids']))
" 2>/dev/null || echo "?")
    echo "🔍 知识库状态: ${ACTIVE_COUNT} 个 active chunks"
else
    echo "🔍 知识库状态: ${ACTIVE_COUNT} 个 active chunks"

    if [ "${NON_INTERACTIVE}" != "1" ]; then
        # Q1: wipe 重灌（每次询问，默认 N；危险操作入口保持可见）
        read -r -p "🗑️  是否 wipe 重灌向量库？[y/N]（回车=否） " ANS || ANS=""
        if [[ "$ANS" =~ ^[Yy]$ ]]; then
            echo "💾 wipe 自动备份 workspace/cache.db（评估历史按铁律 10 保留）"
            .venv/bin/python scripts/ingest_corpus.py --wipe 2>/dev/null \
                | grep -vE "ppocr DEBUG|Namespace" || true
            echo "🔍 验收:"
            HF_HUB_OFFLINE=1 .venv/bin/python scripts/verify_corpus.py 2>/dev/null \
                | grep -E "^  \[|全部通过|失败" || true
        fi

        # Q2: 增量语料
        read -r -p "📄 有增量语料要入库吗？[y/N]（回车=否） " ANS || ANS=""
        if [[ "$ANS" =~ ^[Yy]$ ]]; then
            echo "📁 请将文档放入 assets/corpus/（pdf/docx/md/txt，可建子目录），完成后按回车..."
            read -r || true
            BEFORE="$ACTIVE_COUNT"
            .venv/bin/python scripts/ingest_corpus.py 2>/dev/null \
                | grep -vE "ppocr DEBUG|Namespace" || true
            AFTER=$(.venv/bin/python -c "
from core.config import ConfigRegistry
from storage.chroma_client import ChromaStore
ConfigRegistry.init('config.yaml')
s = ChromaStore()
print(len(s.collection.get(where={'is_active': True})['ids']))
" 2>/dev/null || echo "?")
            DELTA=$((AFTER - BEFORE))
            if [ "$DELTA" -ge 0 ] 2>/dev/null; then
                echo "📊 chunk 变化: ${BEFORE} → ${AFTER} (+${DELTA})"
            else
                echo "📊 chunk 变化: ${BEFORE} → ${AFTER} (${DELTA})"
            fi
        fi
    fi
fi

# ── ③ 启动服务 ──────────────────────────────────────────
echo "🚀 启动 RAG QA Service..."
# stdout/stderr 分别落盘 workspace/logs/run-<时间戳>.log / .stderr.log，
# Maintenance > Logs 页即可查看 chat 请求日志（/logs 接口读该目录）
RUN_LOG="workspace/logs/run-$(date +%Y%m%d-%H%M%S)"
.venv/bin/uvicorn api.app:app --host 0.0.0.0 --port 8000 > "${RUN_LOG}.log" 2> "${RUN_LOG}.stderr.log" &
SERVER_PID=$!

READY=0
for i in $(seq 1 300); do
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        READY=1
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        break
    fi
    if [ $((i % 30)) -eq 0 ]; then
        echo "  ⏳ 仍在启动中…（首次运行需加载模型，请耐心等待）"
    fi
    sleep 1
done

if [ "$READY" = "1" ]; then
    echo "✅ 服务已就绪: http://localhost:8000"
    echo "   演示 UI:     http://localhost:8000/rag-gen-ai-service-demo"
    echo "   API 文档:   http://localhost:8000/docs"
    echo "   健康检查:   http://localhost:8000/health"
    echo "   服务日志:   ${RUN_LOG}.log（stderr: ${RUN_LOG}.stderr.log）"
    echo "   评估日志:   workspace/logs/（eval-*.log，Maintenance > Logs 可选）"
    echo "   Ctrl+C 停止服务"
else
    echo "❌ 服务启动失败或超时，请检查日志后重试" >&2
    exit 1
fi
wait "$SERVER_PID"
