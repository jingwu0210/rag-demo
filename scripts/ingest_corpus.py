"""语料入库脚本：按语料矩阵顺序 ingest 全部 10 个文档。

用法: HF_ENDPOINT=https://hf-mirror.com .venv/bin/python scripts/ingest_corpus.py
（首次运行会下载 BGE-M3 模型；扫描件会触发 PaddleOCR）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import ConfigRegistry
from core.logging_config import setup_logging
from services.ingest import IngestService
from storage.sqlite_client import init_db
import asyncio

# (文件, doc_type, version, effective_date, doc_group) — 顺序敏感：v1.0 先、v1.1 后
CORPUS = [
    ("employee_handbook_v1.0.pdf", "handbook", "v1.0", None, "employee_handbook"),
    ("employee_handbook_v1.1.pdf", "handbook", "v1.1", None, "employee_handbook"),  # 同 doc_group → 替换 v1.0
    ("compliance_guide_cn.pdf", "compliance", "v2.0", None, None),
    ("compliance_guide_en.pdf", "compliance", "v2.0", None, None),
    ("api_specification.md", "technical", "v3.2", None, None),
    ("it_security_policy.pdf", "technical", "v1.4", None, None),
    ("legacy_tech_manual_v2022.pdf", "technical", "v1.0", "2022-01-01", None),  # 过期埋点
    ("architecture_overview.md", "architecture", "v2.1", None, None),
    ("incident_response_plan.pdf", "architecture", "v3.0", None, None),
    ("scanned_hr_notice.pdf", "handbook", "v1.0", None, None),  # 扫描件 → OCR
]


def main():
    ConfigRegistry.init("config.yaml")
    setup_logging()
    asyncio.run(init_db())

    svc = IngestService()  # 重组件（Embedder/ChromaStore/BM25）惰性构建
    corpus_dir = ConfigRegistry.get("paths.corpus", "data/corpus")

    print(f"{'文档':<32} {'状态':<10} {'新建':<6} {'替换':<6} {'版本':<8}")
    print("-" * 72)
    for fname, doc_type, version, eff_date, group in CORPUS:
        fpath = os.path.join(corpus_dir, fname)
        if not os.path.exists(fpath):
            print(f"{fname:<32} {'MISSING':<10}")
            continue
        result = svc.ingest(fpath, doc_type, version,
                            effective_date=eff_date, doc_group=group)
        print(f"{fname:<32} {result.status:<10} "
              f"{result.chunks_created:<6} {result.chunks_replaced:<6} {result.version:<8}")

    print("-" * 72)
    print("入库完成。验证: .venv/bin/python scripts/verify_corpus.py")


if __name__ == "__main__":
    main()
