"""structlog JSON logging with secret redaction and daily file rotation."""

from __future__ import annotations

import logging
import logging.handlers
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import structlog

REDACTED = "***REDACTED***"
_SENSITIVE_KEY = re.compile(r"(secret|token|api_?key|^key$|^sign$|signature|password|authorization)", re.I)


class SecretRedactor:
    """structlog processor: masks sensitive keys and any known secret value
    appearing anywhere in the event (including nested dicts/lists/strings)."""

    def __init__(self, secrets: Iterable[str] = ()):
        # Ignore very short values: they'd cause false-positive masking.
        self._secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            for s in self._secrets:
                if s in value:
                    value = value.replace(s, REDACTED)
            return value
        if isinstance(value, dict):
            return {k: (REDACTED if isinstance(k, str) and _SENSITIVE_KEY.search(k) else self._scrub(v))
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self._scrub(v) for v in value)
        return value

    def __call__(self, logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        return self._scrub(event_dict)


def configure_logging(level: str = "INFO", log_dir: str | None = "logs",
                      secrets: Iterable[str] = ()) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.TimedRotatingFileHandler(
            Path(log_dir) / "agent.log", when="midnight", backupCount=30, encoding="utf-8", utc=False,
        ))
    logging.basicConfig(format="%(message)s", level=level.upper(), handlers=handlers, force=True)
    # httpx logs full URLs at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            SecretRedactor(secrets),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )
