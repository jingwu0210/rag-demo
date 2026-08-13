from datetime import datetime, timedelta
from core.config import ConfigRegistry

class MetadataFilter:
    """doc_type 仅作 metadata 标记（评估分析用），检索全库不过滤。

    v1.6 变更：删除关键词分类（classify）— 静态关键词表表达"query 属于哪个
    文档类型"不可维护且无法验证正确性（评估实测："安全"一词把 technical 的
    密码策略问题误路由到 compliance → it_security_policy 被过滤 → 系统性
    检索失败）。语料规模小（~15 chunks），全库检索无性能压力。

    扩展点：日后语料规模增大需要范围收敛时，可在此重新引入分类
    （LLM 分类 / 加权方案），而非人工关键词表。
    """

    @classmethod
    def classify(cls, query: str) -> str:
        """v1.6 起恒返回 "general"（全库检索）。

        保留方法签名以兼容调用方；query 参数不再使用。
        """
        return "general"

    @classmethod
    def get_doc_types(cls, category: str) -> list:
        """v1.6 起恒返回空列表（不过滤）。保留签名兼容调用方。"""
        return []

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
