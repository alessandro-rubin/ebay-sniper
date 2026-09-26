"""Logging configuration with secret redaction.

The Telegram bot token is part of every Bot API URL, so any log line or
traceback that mentions a request URL would leak it. Every registered secret is
replaced in the fully formatted record, tracebacks included.
"""

from __future__ import annotations

import logging
import sys

_REDACTED = "[REDACTED]"
# Shorter values are not redacted: they would mask unrelated text.
_MIN_SECRET_LENGTH = 8
_secrets: set[str] = set()


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in _secrets:
            text = text.replace(secret, _REDACTED)
        return text


def register_secrets(*values: str) -> None:
    """Redact these values from every log record from now on."""
    _secrets.update(value for value in values if len(value) >= _MIN_SECRET_LENGTH)


def configure_logging(*, verbose: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # httpx logs every request URL at INFO level, Telegram token included.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
