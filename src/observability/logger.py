"""Structured logging with structlog.

Security: Private keys, secrets, and credentials are NEVER logged.
All log output is JSON-formatted for machine parsing in production.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

import structlog


_CONFIGURED = False

# Fields that must NEVER appear in logs
_REDACTED_FIELDS = frozenset({
    "private_key", "secret", "password", "api_secret",
    "passphrase", "api_passphrase", "token", "mnemonic",
    "polymarket_private_key", "polymarket_api_secret",
    "polymarket_api_passphrase",
})


def _redact_processor(
    logger: structlog.types.WrappedLogger,
    method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Remove sensitive fields from log events."""
    for key in list(event_dict.keys()):
        if key.lower() in _REDACTED_FIELDS:
            event_dict[key] = "***REDACTED***"
    return event_dict


def configure_logging(
    level: str = "INFO",
    fmt: str = "json",
    log_file: str | None = None,
) -> None:
    """Configure structured logging for the application."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)

    # Standard-library root logger
    root = logging.getLogger()
    root.setLevel(log_level)

    # Console handler
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(log_level)
    root.addHandler(console)

    # File handler with rotation (optional)
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            str(log_path),
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,              # Keep 5 rotated files
        )
        fh.setLevel(log_level)
        root.addHandler(fh)

    # structlog pipeline
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _redact_processor,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if fmt == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )
    for handler in root.handlers:
        handler.setFormatter(formatter)

    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog bound logger."""
    if not _CONFIGURED:
        configure_logging(
            level=os.environ.get("LOG_LEVEL", "INFO"),
            fmt=os.environ.get("LOG_FORMAT", "console"),
        )
    return structlog.get_logger(name)
