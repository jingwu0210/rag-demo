import re

class BilingualHandler:
    @staticmethod
    def detect(text: str) -> str:
        cn_chars = len(re.findall(r'[一-鿿]', text))
        en_chars = len(re.findall(r'[a-zA-Z]', text))
        if cn_chars == 0:
            return "en"
        if en_chars == 0:
            return "zh"
        return "zh" if cn_chars >= en_chars else "en"

    @staticmethod
    def tag_chunks(chunks: list) -> list:
        for c in chunks:
            if isinstance(c, dict) and "text" in c:
                c["language"] = BilingualHandler.detect(c["text"])
        return chunks
