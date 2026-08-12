import structlog
from core.config import ConfigRegistry

def setup_logging() -> None:
    log_level = ConfigRegistry.get("logging.level", "INFO")
    log_dir = ConfigRegistry.get("logging.log_dir", "data/logs")

    processors = [
        structlog.contextvars.merge_contextvars,   # 支持 API 层 request_id 上下文注入
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
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
