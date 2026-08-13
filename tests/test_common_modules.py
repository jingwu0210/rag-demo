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

def test_metadata_filter_classify():
    from core.config import ConfigRegistry
    from core.metadata import MetadataFilter
    ConfigRegistry.init("config.yaml")
    assert MetadataFilter.classify("年假怎么算") == "handbook"
    assert MetadataFilter.classify("合规要求是什么") == "compliance"
    assert MetadataFilter.classify("API 架构") == "technical"
    assert MetadataFilter.classify("今天天气如何") == "general"

def test_metadata_filter_get_doc_types():
    from core.config import ConfigRegistry
    from core.metadata import MetadataFilter
    ConfigRegistry.init("config.yaml")
    assert MetadataFilter.get_doc_types("handbook") == ["handbook"]
    assert MetadataFilter.get_doc_types("general") == []

def test_expire_filter_disabled_by_default():
    from core.config import ConfigRegistry
    from core.metadata import ExpireFilter
    ConfigRegistry.init("config.yaml")
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
