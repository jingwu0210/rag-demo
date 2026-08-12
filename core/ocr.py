import fitz  # PyMuPDF
from core.config import ConfigRegistry
from core.bilingual import BilingualHandler

class ParsedDoc:
    def __init__(self, text: str, pages: list, tables: list, source: str, language: str):
        self.text = text
        self.pages = pages
        self.tables = tables
        self.source = source
        self.language = language

class OCRPipeline:
    """PDF 解析 + OCR。扫描件检测：逐页 text_ratio < 1% 视为扫描页，走 PaddleOCR。"""

    def process(self, file_path: str) -> ParsedDoc:
        doc = fitz.open(file_path)
        all_text, pages_data, tables_data = [], [], []
        need_ocr = ConfigRegistry.get("ocr.engine", "paddleocr")
        clean_cfg = ConfigRegistry.get("ocr.clean", {})

        for page_num, page in enumerate(doc):
            text = page.get_text("text")
            text_ratio = len(text.strip()) / max(page.rect.width * page.rect.height, 1)

            if text_ratio < 0.01 and need_ocr:
                text = self._ocr_page(page, page_num)
            else:
                text = self._extract_native(page)

            if clean_cfg.get("remove_header_footer", True):
                text = self._remove_header_footer(text)
            if clean_cfg.get("normalize_whitespace", True):
                text = self._normalize_whitespace(text)
            if clean_cfg.get("fix_cn_en_spacing", True):
                text = self._fix_cn_en_spacing(text)

            all_text.append(text)
            pages_data.append({"num": page_num, "text": text})

        full_text = "\n\n".join(all_text)
        language = BilingualHandler.detect(full_text)
        doc.close()
        return ParsedDoc(text=full_text, pages=pages_data, tables=tables_data, source=file_path, language=language)

    def _extract_native(self, page) -> str:
        blocks = page.get_text("blocks")
        blocks.sort(key=lambda b: (b[1], b[0]))
        return "\n".join(b[4] for b in blocks if b[4].strip())

    def _ocr_page(self, page, page_num: int) -> str:
        from paddleocr import PaddleOCR
        dpi = ConfigRegistry.get("ocr.dpi", 300)
        pix = page.get_pixmap(dpi=dpi)
        img_path = f"data/ocr/page_{page_num}.png"
        pix.save(img_path)
        ocr = PaddleOCR(lang=ConfigRegistry.get("ocr.language", ["ch", "en"]), use_angle_cls=True)
        result = ocr.ocr(img_path, cls=True)
        if result and result[0]:
            return "\n".join(line[1][0] for line in result[0])
        return ""

    def _remove_header_footer(self, text: str) -> str:
        lines = text.strip().split("\n")
        if len(lines) <= 3:
            return text
        return "\n".join(lines[1:-1])

    def _normalize_whitespace(self, text: str) -> str:
        import re
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[ \t]{2,}', ' ', text)
        return text.strip()

    def _fix_cn_en_spacing(self, text: str) -> str:
        import re
        text = re.sub(r'([一-鿿])([a-zA-Z0-9])', r'\1 \2', text)
        text = re.sub(r'([a-zA-Z0-9])([一-鿿])', r'\1 \2', text)
        return text
