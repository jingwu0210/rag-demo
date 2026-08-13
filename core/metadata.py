from datetime import datetime, timedelta
from core.config import ConfigRegistry

class MetadataFilter:
    @classmethod
    def classify(cls, query: str) -> str:
        doc_types = ConfigRegistry.get("doc_types", {})
        for category, cfg in doc_types.items():
            for kw in cfg.get("keywords", []):
                if kw.lower() in query.lower():
                    return category
        return "general"

    @classmethod
    def get_doc_types(cls, category: str) -> list:
        if category == "general":
            return []
        return ConfigRegistry.get(f"doc_types.{category}.doc_type", [])

class ExpireFilter:
    def __init__(self):
        self.enabled = ConfigRegistry.get("retrieval.metadata_filter.expire.enabled", False)
        self.grace_days = ConfigRegistry.get("retrieval.metadata_filter.expire.grace_period_days", 90)

    def get_where_clause(self):
        if not self.enabled:
            return None
        # chromadb 0.5.23 的 $gte 只接受 int/float → effective_date 存整数 YYYYMMDD
        cutoff = int((datetime.now() - timedelta(days=self.grace_days)).strftime("%Y%m%d"))
        return {"effective_date": {"$gte": cutoff}}
