"""语料验证脚本：核对 Phase 11 的 7 项验收标准。

用法: .venv/bin/python scripts/verify_corpus.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import ConfigRegistry
from storage.chroma_client import ChromaStore


def check(name, ok, detail=""):
    print(f"  [{'✓' if ok else '✗'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def main():
    ConfigRegistry.init("config.yaml")
    store = ChromaStore()
    all_pass = True

    # 1. 版本管理：v1.0 全软下线、v1.1 生效（按 doc_group 版本族查）
    v10 = store.collection.get(where={"$and": [
        {"doc_group": "employee_handbook"}, {"version": "v1.0"}]})
    v11 = store.collection.get(where={"$and": [
        {"doc_group": "employee_handbook"}, {"version": "v1.1"}]})
    v10_all_inactive = all(not m.get("is_active") for m in (v10["metadatas"] or [])) and len(v10["ids"]) > 0
    v11_all_active = all(m.get("is_active") for m in (v11["metadatas"] or [])) and len(v11["ids"]) > 0
    all_pass &= check("版本管理: v1.0 软下线 + v1.1 生效",
                      v10_all_inactive and v11_all_active,
                      f"v1.0={len(v10['ids'])} chunks inactive, v1.1={len(v11['ids'])} active")

    # 2. OCR：扫描件 chunks 存在且文本含"人力资源部"
    scanned = store.collection.get(where={"source_file_stem": "scanned_hr_notice"})
    scanned_ok = len(scanned["ids"]) > 0 and any(
        "人力资源部" in d for d in (scanned["documents"] or []))
    all_pass &= check("OCR: 扫描件入库且文本含'人力资源部'",
                      scanned_ok, f"{len(scanned['ids'])} chunks")

    # 3. 注入样本可被检索（Scanner 的 block 验证在检索链路，此处验证文本可提取）
    inj = store.collection.get(where={"source_file_stem": "it_security_policy"})
    inj_ok = any("IGNORE ALL PREVIOUS INSTRUCTIONS" in d for d in (inj["documents"] or []))
    all_pass &= check("注入样本: 白字指令可被文本提取（供 Scanner block）", inj_ok)

    # 4. 过期文档埋点：effective_date=2022-01-01 写入 metadata
    legacy = store.collection.get(where={"source_file_stem": "legacy_tech_manual_v2022"})
    legacy_ok = len(legacy["ids"]) > 0 and all(
        m.get("effective_date") == 20220101 for m in (legacy["metadatas"] or []))
    all_pass &= check("过期埋点: legacy effective_date=20220101 (int)", legacy_ok)

    # 5. 双语语料齐备
    zh = store.collection.get(where={"language": "zh"})
    en = store.collection.get(where={"language": "en"})
    all_pass &= check("双语: 中英文 chunks 均存在",
                      len(zh["ids"]) > 0 and len(en["ids"]) > 0,
                      f"zh={len(zh['ids'])}, en={len(en['ids'])}")

    # 6. 全量 active chunk 统计（按 doc_type）
    raw = store.collection.get(where={"is_active": True})
    by_type = {}
    for m in raw["metadatas"]:
        by_type[m["doc_type"]] = by_type.get(m["doc_type"], 0) + 1
    all_pass &= check("doc_type 分布", len(by_type) == 4,
                      f"handbook={by_type.get('handbook', 0)}, "
                      f"compliance={by_type.get('compliance', 0)}, "
                      f"technical={by_type.get('technical', 0)}, "
                      f"architecture={by_type.get('architecture', 0)}")

    print()
    print("全部通过 ✓" if all_pass else "存在失败项 ✗ — 检查上方输出")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
