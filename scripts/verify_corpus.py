"""语料验证脚本：核对新语料 baseline 的 7 项验收标准。

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

    # 1. 版本管理：v2025 全软下线、2026.0 生效（doc_group=employee-handbook.cn）
    grp = store.collection.get(where={"doc_group": "employee-handbook.cn"})
    metas = grp["metadatas"] or []
    v2025_inactive = all(not m.get("is_active")
                         for m in metas if m.get("version") == "v2025")
    v2025_count = sum(1 for m in metas if m.get("version") == "v2025")
    active_current = [m for m in metas if m.get("is_active")]
    active_all_current = all(m.get("version") == "2026.0" for m in active_current) \
        and len(active_current) > 0
    all_pass &= check("版本管理: v2025 软下线 + 2026.0 生效",
                      v2025_inactive and v2025_count > 0 and active_all_current,
                      f"v2025={v2025_count} 全 inactive, active 2026.0={len(active_current)}")

    # 2. OCR：扫描件入库且文本可读（api-doc.scan.pdf 经 PaddleOCR）
    scanned = store.collection.get(where={"source_file_stem": "api-doc.scan"})
    scanned_ok = len(scanned["ids"]) > 0 and any(
        "API" in d and "Bearer" in d for d in (scanned["documents"] or []))
    all_pass &= check("OCR: 扫描件入库且文本含 API/Bearer",
                      scanned_ok, f"{len(scanned['ids'])} chunks")

    # 3. 注入样本：payload 文本可提取（PDF 提取字母间距化，去空格后匹配）
    inj = store.collection.get(where={"source_file_stem": "injection-sample.en"})
    inj_ok = any("ignoreallpreviousinstructions" in d.replace(" ", "").lower()
                 for d in (inj["documents"] or []))
    all_pass &= check("注入样本: payload 可被文本提取（供 Scanner 拦截）",
                      inj_ok, f"{len(inj['ids'])} chunks")

    # 4. 过期文档埋点：effective_date=20190601（int）写入 metadata
    legacy = store.collection.get(where={"source_file_stem": "legacy-travel-policy.cn"})
    legacy_ok = len(legacy["ids"]) > 0 and all(
        m.get("effective_date") == 20190601 for m in (legacy["metadatas"] or []))
    all_pass &= check("过期埋点: legacy effective_date=20190601 (int)", legacy_ok)

    # 5. 双语语料齐备
    zh = store.collection.get(where={"language": "zh"})
    en = store.collection.get(where={"language": "en"})
    all_pass &= check("双语: 中英文 chunks 均存在",
                      len(zh["ids"]) > 0 and len(en["ids"]) > 0,
                      f"zh={len(zh['ids'])}, en={len(en['ids'])}")

    # 6. 全量 active chunk 统计（按 doc_type，4 类齐备）
    raw = store.collection.get(where={"is_active": True})
    by_type = {}
    for m in raw["metadatas"]:
        by_type[m["doc_type"]] = by_type.get(m["doc_type"], 0) + 1
    all_pass &= check("doc_type 分布（4 类齐备）", len(by_type) == 4,
                      f"handbook={by_type.get('handbook', 0)}, "
                      f"compliance={by_type.get('compliance', 0)}, "
                      f"technical={by_type.get('technical', 0)}, "
                      f"architecture={by_type.get('architecture', 0)}")

    # 7. 格式覆盖：md / 文本型 pdf / 扫描 pdf / docx 四路处理路径均有真实语料
    exts = {m.get("source_file", "").rsplit(".", 1)[-1]
            for m in (store.collection.get()["metadatas"] or [])}
    fmt_ok = {"md", "pdf", "docx"}.issubset(exts)
    all_pass &= check("格式覆盖: md/pdf/docx 三扩展名均有 chunk",
                      fmt_ok, f"extensions={sorted(exts)}")

    print()
    print("全部通过 ✓" if all_pass else "存在失败项 ✗ — 检查上方输出")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
