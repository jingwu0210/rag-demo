"""分层语义分片器（HierarchicalChunker），按设计文档 §5.2 的 7 步算法实现：

1. 标题层级切分：config chunking.heading_patterns（markdown # / 第X章 / Chapter/Section/Article / §）
   识别标题行，把全文切成 section 树，每个 chunk 记录 heading_path（如 "员工手册 > 第三章 薪酬福利 > 年假"）。
2. 段落边界切分：section 超过 max_chunk_tokens 时按空行分段落，贪心合并直到逼近 max_chunk_tokens。
3. overlap：段落间切换 chunk 时，携带上一 chunk 末尾 overlap_tokens 个 token 作为重叠前缀。
4. heading_context：chunk.text 前缀注入 [{heading_path}]（config chunking.heading_context=true 时）。
5. min_chunk_tokens：小于 min_chunk_tokens 的 chunk 合并到上一 chunk。
6. token 计数：启发式近似——中文 1 字符 ≈ 1 token，英文 1 词 ≈ 1.3 token（见 _token_count 注释）。
7. chunk_id：uuid4 hex；metadata 初始化含 source_file（仅文件名）/heading_path/language/chunk_index。
"""
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from core.config import ConfigRegistry
from core.ocr import ParsedDoc


@dataclass
class Chunk:
    chunk_id: str
    text: str
    heading_path: str
    metadata: dict = field(default_factory=dict)
    embedding: Optional[List[float]] = None
    language: str = "unknown"


# 可逆的近似 token 切分：中文单字符 / 英文数字词 / 标点 / 空白分别成段，
# 段按原序拼接后与原文逐字符一致，因此可安全用于 overlap 提取与超长硬切
_TOKEN_SEG_RE = re.compile(r"[一-鿿]|[a-zA-Z0-9]+|[^一-鿿\sa-zA-Z0-9]|\s+")


class HierarchicalChunker:
    def __init__(self):
        self.max_chunk_tokens = ConfigRegistry.get("chunking.max_chunk_tokens", 512)
        self.min_chunk_tokens = ConfigRegistry.get("chunking.min_chunk_tokens", 100)
        self.overlap_tokens = ConfigRegistry.get("chunking.overlap_tokens", 64)
        self.heading_context = ConfigRegistry.get("chunking.heading_context", True)
        self.heading_patterns = ConfigRegistry.get("chunking.heading_patterns", [])

    # ── 入口 ──────────────────────────────────────────────
    def chunk(self, doc: ParsedDoc) -> List[Chunk]:
        """Step 1/2/3/7：标题切分 → 段落切分（含 overlap）→ 生成 Chunk"""
        source_name = os.path.basename(doc.source) if doc.source else ""
        chunks: List[Chunk] = []
        for heading_path, body in self._split_by_headings(doc.text):
            if self._token_count(body) <= self.max_chunk_tokens:
                pieces = [body]
            else:
                pieces = self._split_by_paragraphs(body)
            for text in pieces:
                chunk = Chunk(
                    chunk_id=uuid.uuid4().hex,
                    text=text,
                    heading_path=heading_path,
                    language=doc.language,
                    metadata={
                        "source_file": source_name,
                        "heading_path": heading_path,
                        "language": doc.language,
                        "chunk_index": len(chunks),
                    },
                )
                chunks.append(chunk)

        # Step 4：heading_context 注入（无标题路径的根内容不注入，避免 "[]" 前缀）
        if self.heading_context:
            for c in chunks:
                if c.heading_path:
                    c.text = f"[{c.heading_path}]\n{c.text}"

        # Step 5：小于 min_chunk_tokens 的 chunk 合并到上一 chunk（保留前者的标题身份）
        chunks = self._merge_min_chunks(chunks)
        # Step 8：合并后重排 chunk_index，保持 0..n-1 连续
        for i, c in enumerate(chunks):
            c.metadata["chunk_index"] = i
        return chunks

    # ── Step 1: 标题层级切分 ───────────────────────────────
    def _split_by_headings(self, text: str) -> List[Tuple[str, str]]:
        """按标题行把全文切成 [(heading_path, body)]；标题行保留在 section 正文首行。
        层级栈规则：新标题出现时弹出所有 level >= 新标题 level 的祖先（同级/下级标题开启新章节）。"""
        lines = text.split("\n")
        sections: List[Tuple[str, str]] = []
        stack: List[Tuple[int, str]] = []  # (level, title)
        cur_lines: List[str] = []

        def path() -> str:
            return " > ".join(title for _, title in stack)

        for line in lines:
            matched = self._match_heading(line)
            if matched:
                level, title = matched
                body = "\n".join(cur_lines).strip()
                if body:
                    sections.append((path(), body))
                cur_lines = []
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                cur_lines.append(line)  # 标题文本进正文首行，语义更完整
            else:
                cur_lines.append(line)

        body = "\n".join(cur_lines).strip()
        if body:
            sections.append((path(), body))
        return sections

    def _match_heading(self, line: str) -> Optional[Tuple[int, str]]:
        """用 config 的 heading_patterns 识别标题行 → (层级, 标题)。
        层级约定（与 patterns 顺序绑定，见 config.yaml）：
          [0] markdown #: 层级 = # 数量；[1] 第X章/节/条: 1 级；
          [2] Chapter: 1 / Section: 2 / Article: 3；[3] §: 2 级。"""
        s = line.strip()
        if not s:
            return None
        for idx, pattern in enumerate(self.heading_patterns):
            if re.match(pattern, s):
                if idx == 0:  # ^#{1,6}\s
                    level = len(s) - len(s.lstrip("#"))
                    title = s.lstrip("#").strip()
                elif idx == 1:  # 第X章/节/条
                    level, title = 1, s
                elif idx == 2:  # Chapter/Section/Article
                    m = re.match(r"^(Chapter|Section|Article)", s)
                    level = {"Chapter": 1, "Section": 2, "Article": 3}.get(m.group(1), 2)
                    title = s
                else:  # ^§\s*\d+
                    level, title = 2, s
                return level, title
        return None

    # ── Step 2/3: 段落边界切分 + overlap ──────────────────
    def _split_by_paragraphs(self, section_text: str) -> List[str]:
        """长 section 按空行分段落，贪心合并逼近 max_chunk_tokens；
        满员输出 chunk 时保留上一 chunk 末尾 overlap_tokens 个 token 作为下一 chunk 前缀。"""
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", section_text) if p.strip()]
        out: List[str] = []
        buf: List[str] = []
        buf_tokens = 0

        def flush() -> str:
            nonlocal buf, buf_tokens
            if not buf:
                return ""
            chunk_text = "\n".join(buf)
            out.append(chunk_text)
            overlap = self._get_last_n_tokens(chunk_text, self.overlap_tokens)
            buf, buf_tokens = [], 0
            return overlap

        for para in paragraphs:
            para_len = self._token_count(para)
            if para_len > self.max_chunk_tokens:
                # 单段落超限（罕见）：按 token 窗口硬切后继续贪心合并
                for piece in self._hard_split(para):
                    if buf and buf_tokens + self._token_count(piece) > self.max_chunk_tokens:
                        overlap = flush()
                        if overlap:
                            buf.append(overlap)
                            buf_tokens = self._token_count(overlap)
                    buf.append(piece)
                    buf_tokens += self._token_count(piece)
            elif buf and buf_tokens + para_len > self.max_chunk_tokens:
                overlap = flush()
                if overlap:
                    buf.append(overlap)
                    buf_tokens = self._token_count(overlap)
                buf.append(para)
                buf_tokens += para_len
            else:
                buf.append(para)
                buf_tokens += para_len

        if buf:
            out.append("\n".join(buf))
        return out

    def _hard_split(self, text: str) -> List[str]:
        """超长段落按近似 token 窗口硬切（fallback，正常文本不会触发）"""
        segs = self._token_segments(text)
        parts: List[str] = []
        cur: List[str] = []
        cur_tokens = 0.0
        for seg in segs:
            weight = self._seg_weight(seg)
            if cur and cur_tokens + weight > self.max_chunk_tokens:
                parts.append("".join(cur))
                cur, cur_tokens = [], 0.0
            cur.append(seg)
            cur_tokens += weight
        if cur:
            parts.append("".join(cur))
        return parts

    # ── Step 5: 小 chunk 合并 ─────────────────────────────
    def _merge_min_chunks(self, chunks: List[Chunk]) -> List[Chunk]:
        """小于 min_chunk_tokens 的 chunk 并入上一 chunk（保留上一 chunk 的 heading_path）"""
        merged: List[Chunk] = []
        for c in chunks:
            if merged and self._token_count(c.text) < self.min_chunk_tokens:
                merged[-1].text += "\n\n" + c.text
            else:
                merged.append(c)
        return merged

    # ── Step 6: 近似 token 计数 ───────────────────────────
    def _token_count(self, text: str) -> int:
        """启发式近似 token 计数：中文 1 字符 ≈ 1 token，英文 1 词 ≈ 1.3 token。

        说明：理想方案是使用 BGE-M3 的 tokenizer（SentenceTransformer(model).tokenizer），
        但模型首次加载需联网下载（国内需 HF_ENDPOINT 镜像），且 ingest 为离线冷路径、
        对分片边界精度不敏感，故采用近似计数，误差仅影响边界位置，不影响正确性。
        """
        cn = len(re.findall(r"[一-鿿]", text))
        en = len(re.findall(r"[a-zA-Z0-9]+", text))
        return cn + int(en * 1.3)

    def _token_segments(self, text: str) -> List[str]:
        return [m.group(0) for m in _TOKEN_SEG_RE.finditer(text)]

    @staticmethod
    def _seg_weight(seg: str) -> float:
        if re.fullmatch(r"[一-鿿]", seg):
            return 1.0
        if re.fullmatch(r"[a-zA-Z0-9]+", seg):
            return 1.3
        if re.fullmatch(r"\s+", seg):
            return 0.0
        return 1.0  # 标点近似 1

    def _get_last_n_tokens(self, text: str, n: int) -> str:
        """取文本末尾约 n 个 token（按近似权重累加），返回可拼接回原文的字符串"""
        if n <= 0 or not text:
            return ""
        segs = self._token_segments(text)
        acc = 0.0
        tail: List[str] = []
        for seg in reversed(segs):
            tail.append(seg)
            acc += self._seg_weight(seg)
            if acc >= n:
                break
        return "".join(reversed(tail))
