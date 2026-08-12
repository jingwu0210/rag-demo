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
