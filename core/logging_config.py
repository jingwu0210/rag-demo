import structlog
from core.config import ConfigRegistry


def _reorder_fields(logger, method_name, event_dict):
    """字段重排：timestamp → level → event → module → 其余（行业 JSON 行日志惯例，
    Go zap / Java logback 风格头部固定，业务字段分组在后）"""
    ordered = {}
    for key in ("timestamp", "level", "event", "module"):
        if key in event_dict:
            ordered[key] = event_dict.pop(key)
    ordered.update(event_dict)
    return ordered


def setup_logging() -> None:
    log_level = ConfigRegistry.get("logging.level", "INFO")
    log_dir = ConfigRegistry.get("logging.log_dir", "data/logs")

    processors = [
        structlog.contextvars.merge_contextvars,   # 支持 API 层 request_id 上下文注入
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _reorder_fields,                           # 固定键序重排（timestamp 行首）
    ]

    if ConfigRegistry.get("logging.format", "json") == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

def get_logger(**kwargs) -> structlog.BoundLogger:
    return structlog.get_logger(**kwargs)


def get_request_id():
    """从 structlog contextvars 读当前请求 id（API 层 bind_contextvars 注入）。

    直接调用场景（eval/smoke）无绑定 → None，靠 run_id 追踪。
    """
    from structlog.contextvars import get_contextvars
    return get_contextvars().get("request_id")
