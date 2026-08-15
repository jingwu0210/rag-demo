"""语料入库脚本：自动扫描 corpus 目录增量 ingest（支持单文件与安全 wipe 重灌）。

用法:
  # 增量（默认）: 扫描 assets/corpus/ 所有文件
  #   新文件 → ingested；已有文件 → skipped（hash 相同，零开销）
  #   同 doc_group 新版本 → replaced（旧版软下线）
  HF_ENDPOINT=https://hf-mirror.com .venv/bin/python scripts/ingest_corpus.py

  # 单文件增量（新文档无需改本脚本）
  HF_ENDPOINT=https://hf-mirror.com .venv/bin/python scripts/ingest_corpus.py \
      --file assets/corpus/new_policy.pdf --doc-type handbook --version v1.0

  # 安全 wipe 重灌（格式/参数/模型变更时使用）:
  #   SQLite 表 DELETE（非 rm 文件）+ ChromaDB 集合删除重建 + 数据备份
  HF_ENDPOINT=https://hf-mirror.com .venv/bin/python scripts/ingest_corpus.py --wipe

首次运行会下载 BGE-M3 模型；扫描件会触发 PaddleOCR。
"""
import argparse
import asyncio
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import ConfigRegistry
from core.logging_config import setup_logging
from services.ingest import IngestService
from storage.sqlite_client import init_db

# 元数据映射（filename → doc_type/version/effective_date/doc_group）。
# 顺序敏感：同 doc_group 的新旧版本须按旧→新排列。
CORPUS_META = {
    "employee_handbook_v1.0.pdf": ("handbook", "v1.0", None, "employee_handbook"),
    "employee_handbook_v1.1.pdf": ("handbook", "v1.1", None, "employee_handbook"),
    "compliance_guide_cn.pdf": ("compliance", "v2.0", None, None),
    "compliance_guide_en.pdf": ("compliance", "v2.0", None, None),
    "api_specification.md": ("technical", "v3.2", None, None),
    "it_security_policy.pdf": ("technical", "v1.4", None, None),
    "legacy_tech_manual_v2022.pdf": ("technical", "v1.0", "2022-01-01", None),
    "architecture_overview.md": ("architecture", "v2.1", None, None),
    "incident_response_plan.pdf": ("architecture", "v3.0", None, None),
    "scanned_hr_notice.pdf": ("handbook", "v1.0", None, None),
    "injection_sample.pdf": ("technical", "v1.0", None, None),  # 安全测试文档（独立存放，防污染正常文档）
}

DEFAULT_META = ("handbook", "v1.0", None, None)   # 未映射新文件的默认元数据


def wipe_safely():
    """安全 wipe（铁律 10）：备份 → SQLite 表 DELETE → ChromaDB 集合删除重建。

    禁止 rm 数据库文件 — cache.db 是混合库（缓存+评估历史+指标+对话）。
    """
    sqlite_path = ConfigRegistry.get("paths.sqlite", "workspace/cache.db")
    chroma_dir = ConfigRegistry.get("chromadb.persist_directory", "assets/chroma")

    # 1. 备份
    backup_dir = "data/backup"
    os.makedirs(backup_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    if os.path.exists(sqlite_path):
        shutil.copy2(sqlite_path, os.path.join(backup_dir, f"cache-{ts}.db"))
        print(f"SQLite 已备份: data/backup/cache-{ts}.db")

    # 2. SQLite 数据表清空（保留表结构）
    import sqlite3
    conn = sqlite3.connect(sqlite_path)
    for table in ("ingest_log", "cache_entries", "turns", "sessions",
                  "request_metrics", "eval_history"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    print("SQLite 数据表已清空（表结构保留）")

    # 3. ChromaDB 集合删除重建
    from storage.chroma_client import ChromaStore
    store = ChromaStore()
    store._client.delete_collection(store.collection.name)
    store.collection  # 触发 get_or_create_collection 重建
    print("ChromaDB 集合已删除重建")


def ingest_file(svc, fpath, meta):
    doc_type, version, eff_date, group = meta
    fname = os.path.basename(fpath)
    result = svc.ingest(fpath, doc_type, version,
                        effective_date=eff_date, doc_group=group)
    print(f"{fname:<32} {result.status:<10} "
          f"{result.chunks_created:<6} {result.chunks_replaced:<6} {result.version:<8}")
    return result


def ingest_all(svc, corpus_dir):
    # 扫描目录；CORPUS_META 中的文件按声明顺序（版本新旧次序），
    # 未映射的新文件追加在最后（默认元数据）
    files_on_disk = set(os.listdir(corpus_dir))
    ordered = []
    for fname, meta in CORPUS_META.items():
        if fname in files_on_disk:
            ordered.append((fname, meta))
    for fname in sorted(files_on_disk):
        if fname not in CORPUS_META and not fname.startswith("."):
            print(f"注意: {fname} 未在 CORPUS_META 映射，使用默认元数据 "
                  f"{DEFAULT_META[0]}/{DEFAULT_META[1]}")
            ordered.append((fname, DEFAULT_META))

    print(f"{'文档':<32} {'状态':<10} {'新建':<6} {'替换':<6} {'版本':<8}")
    print("-" * 72)
    results = []
    for fname, meta in ordered:
        results.append(ingest_file(svc, os.path.join(corpus_dir, fname), meta))
    print("-" * 72)
    print(f"完成 {len(results)} 个文件。验证: .venv/bin/python scripts/verify_corpus.py")


def main():
    parser = argparse.ArgumentParser(description="语料入库（增量默认 / --file 单文件 / --wipe 重灌）")
    parser.add_argument("--file", help="单文件增量 ingest（可配 --doc-type/--version/--effective-date/--doc-group）")
    parser.add_argument("--doc-type", default=None)
    parser.add_argument("--version", default="v1.0")
    parser.add_argument("--effective-date", default=None)
    parser.add_argument("--doc-group", default=None)
    parser.add_argument("--wipe", action="store_true", help="安全 wipe 后全量重灌")
    args = parser.parse_args()

    ConfigRegistry.init("config.yaml")
    setup_logging()
    asyncio.run(init_db())

    if args.wipe:
        wipe_safely()

    svc = IngestService()
    corpus_dir = ConfigRegistry.get("paths.corpus", "assets/corpus")

    if args.file:
        fpath = args.file
        if not os.path.exists(fpath):
            print(f"文件不存在: {fpath}")
            sys.exit(1)
        fname = os.path.basename(fpath)
        if args.doc_type:
            meta = (args.doc_type, args.version, args.effective_date, args.doc_group)
        else:
            meta = CORPUS_META.get(fname, DEFAULT_META)
            if fname not in CORPUS_META:
                print(f"注意: {fname} 未映射，使用默认元数据 {meta[0]}/{meta[1]}")
        print(f"{'文档':<32} {'状态':<10} {'新建':<6} {'替换':<6} {'版本':<8}")
        print("-" * 72)
        ingest_file(svc, fpath, meta)
        print("-" * 72)
    else:
        ingest_all(svc, corpus_dir)


if __name__ == "__main__":
    main()
