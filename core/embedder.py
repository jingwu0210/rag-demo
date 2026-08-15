from sentence_transformers import SentenceTransformer
from core.config import ConfigRegistry
import numpy as np

class Embedder:
    def __init__(self):
        self.model = SentenceTransformer(
            ConfigRegistry.get("embedding.model", "BAAI/bge-m3"),
            device=ConfigRegistry.get("embedding.device", "mps")
        )
        self.batch_size = ConfigRegistry.get("embedding.batch_size", 32)

    def encode(self, texts: list) -> np.ndarray:
        return self.model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                                 show_progress_bar=False)

    def warmup(self) -> None:
        """首次 encode 触发 MPS GPU 上下文懒初始化（权重加载在 __init__ 完成，但
        PyTorch/MPS 的内存分配与内核编译延迟到首次 forward）。startup 阶段主动预热，
        避免第一条真实请求承担该开销而撞上 retrieval.timeout。"""
        self.encode(["warmup"])
