"""Strukturiertes Logging (JSON auf stdout) mit structlog.

@relation(SWA-CORE-LOG, scope=file)
"""

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    """Richtet structlog und das Standard-Logging einheitlich ein.

    @relation(SWR-CORE-004, scope=function)
    """
    log_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(level=log_level, stream=sys.stdout, format="%(message)s", force=True)
    logging.getLogger("httpx").setLevel(max(log_level, logging.WARNING))


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    """@relation(SWR-CORE-004, scope=function)"""
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
