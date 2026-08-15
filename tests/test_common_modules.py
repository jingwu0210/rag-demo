def test_bilingual_detect():
    from core.bilingual import BilingualHandler
    assert BilingualHandler.detect("你好世界") == "zh"
    assert BilingualHandler.detect("Hello world") == "en"
    assert BilingualHandler.detect("中英混合 text") in ("zh", "en")
    # 空文本
    assert BilingualHandler.detect("") == "en"

def test_bilingual_tag_chunks():
    from core.bilingual import BilingualHandler
    chunks = [{"text": "年假政策"}, {"text": "API spec"}]
    tagged = BilingualHandler.tag_chunks(chunks)
    assert tagged[0]["language"] == "zh"
    assert tagged[1]["language"] == "en"

def test_metadata_filter_classify_v16_always_general():
    """v1.6: 关键词分类删除（不可维护且误路由）→ classify 恒返回 general（全库检索）"""
    from core.config import ConfigRegistry
    from core.metadata import MetadataFilter
    ConfigRegistry.init("config.yaml")
    assert MetadataFilter.classify("年假怎么算") == "general"
    assert MetadataFilter.classify("合规要求是什么") == "general"
    assert MetadataFilter.classify("API 架构") == "general"
    assert MetadataFilter.classify("IT 安全策略中密码要求") == "general"  # 原误路由场景

def test_metadata_filter_get_doc_types_v16_always_empty():
    """v1.6: doc_type 过滤删除 → get_doc_types 恒返回空列表"""
    from core.config import ConfigRegistry
    from core.metadata import MetadataFilter
    ConfigRegistry.init("config.yaml")
    assert MetadataFilter.get_doc_types("handbook") == []
    assert MetadataFilter.get_doc_types("general") == []

def test_expire_filter_enabled_by_default():
    """R13: 过期过滤默认开启（R6 归因：legacy 过期文档排检索第 1 位压过现行标准）"""
    from core.config import ConfigRegistry
    from core.metadata import ExpireFilter
    ConfigRegistry.init("config.yaml")
    ef = ExpireFilter()
    assert ef.enabled is True
    assert ef.get_where_clause() is not None

def test_expire_filter_disabled_returns_none():
    from core.config import ConfigRegistry
    from core.metadata import ExpireFilter
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("retrieval.metadata_filter.expire.enabled", False)
    ef = ExpireFilter()
    assert ef.get_where_clause() is None

def test_expire_filter_enabled():
    from core.config import ConfigRegistry
    from core.metadata import ExpireFilter
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("retrieval.metadata_filter.expire.enabled", True)
    ef = ExpireFilter()
    clause = ef.get_where_clause()
    assert clause is not None
    # chromadb $gte 仅支持数值 → 断言整数 YYYYMMDD
    assert "effective_date" in clause
    cutoff = clause["effective_date"]["$gte"]
    assert isinstance(cutoff, int) and 20250101 <= cutoff <= 20991231
    # 恢复，避免污染其他测试
    ConfigRegistry.override("retrieval.metadata_filter.expire.enabled", False)

def test_logging_setup_and_get():
    from core.config import ConfigRegistry
    from core.logging_config import setup_logging, get_logger
    ConfigRegistry.init("config.yaml")
    setup_logging()
    logger = get_logger(module="test")
    logger.info("test_event", key="value")


def test_log_reorder_processor_key_order():
    """L1: 字段重排 — timestamp/level/event/module 固定行首，其余键保持传入顺序"""
    from core.logging_config import _reorder_fields

    event_dict = {"answer": {"preview": "x"}, "module": "chat",
                  "event": "generation_complete", "level": "info",
                  "timestamp": "2026-01-01T00:00:00Z", "llm": {"provider": "d"}}
    ordered = _reorder_fields(None, None, dict(event_dict))
    keys = list(ordered.keys())
    assert keys[:4] == ["timestamp", "level", "event", "module"]
    assert "llm" in keys and "answer" in keys
    # 嵌套 dict 不被破坏
    assert ordered["llm"] == {"provider": "d"}


def test_log_reorder_missing_header_fields():
    """L1: 头部字段缺失时跳过（如 logger 未带 module）"""
    from core.logging_config import _reorder_fields
    event_dict = {"event": "x", "timestamp": "t", "foo": 1}
    ordered = _reorder_fields(None, None, dict(event_dict))
    assert list(ordered.keys())[:2] == ["timestamp", "event"]
