def test_config_get_and_override():
    from core.config import ConfigRegistry
    ConfigRegistry.init("config.yaml")
    assert ConfigRegistry.get("retrieval.mode") == "hybrid+rerank"
    assert ConfigRegistry.get("llm.provider") == "deepseek"
    # 不存在的键返回 default
    assert ConfigRegistry.get("nonexistent.key", "fallback") == "fallback"
    # override 修改内存值
    ConfigRegistry.override("retrieval.mode", "vector")
    assert ConfigRegistry.get("retrieval.mode") == "vector"

def test_config_nested_override_creates_path():
    from core.config import ConfigRegistry
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("a.b.c", 42)
    assert ConfigRegistry.get("a.b.c") == 42
