"""structlog setup with PII-safe redaction.

Single responsibility: configure structlog to emit JSON logs and drop any sensitive
key from the event dict before rendering. PII never enters the rendered output.

Redaction rule (BUILD_SPEC.md rule 0.6): drop any key matching
cookie|li_at|jsessionid|csrf|authorization|api_key, case-insensitive, prefix or exact.
A redacted key is replaced with the literal "[REDACTED]" so the omission is visible,
not silent.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

# Keys that must never appear in rendered logs. Match case-insensitive on the full key,
# or as a prefix (e.g. "cookie_value", "csrf_token").
_REDACT_KEY = re.compile(
    r"^(cookie|li_at|jsessionid|csrf|authorization|api_key|x-api-key|x-li-token|set-cookie|bearer)",
    re.I,
)


def redact(logger, method_name: str, event_dict: dict) -> dict:  # type: ignore[no-untyped-def]
    """structlog processor: replace sensitive values with [REDACTED] in place."""
    for k in list(event_dict.keys()):
        if _REDACT_KEY.match(k):
            event_dict[k] = "[REDACTED]"
    # Also walk nested dicts (e.g. structlog bind_data={...}).
    _redact_nested(event_dict)
    return event_dict


def _redact_nested(obj: Any) -> None:
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            if _REDACT_KEY.match(k):
                obj[k] = "[REDACTED]"
            else:
                _redact_nested(obj.get(k))
    elif isinstance(obj, list):
        for el in obj:
            _redact_nested(el)


def configure_logging(level: str = "INFO") -> None:
    """Idempotent structlog + stdlib logging configuration for the process.

    Call once at app startup (lifespan). Safe to call again; reconfigures.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # stdlib: route everything through stderr.
    logging.basicConfig(
        stream=sys.stderr,
        level=log_level,
        format="%(message)s",
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a configured structlog logger. Call after configure_logging()."""
    return structlog.get_logger(name)  # type: ignore[no-untyped-def]