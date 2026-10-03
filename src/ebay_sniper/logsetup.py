"""Logging configuration with secret redaction.

The Telegram bot token is part of every Bot API URL, so any log line or
traceback that mentions a request URL would leak it. Every registered secret is
replaced in the fully formatted record, tracebacks included.

Records go to stderr or, for a scheduler, only to a size-rotated UTF-8 file:
Windows Task Scheduler does not redirect output, and cron would mail every
line written to stderr.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_REDACTED = "[REDACTED]"
# Shorter values are not redacted: they would mask unrelated text.
_MIN_SECRET_LENGTH = 8
# The log file never grows beyond 4 MB on disk (current file plus three old ones).
_LOG_FILE_MAX_BYTES = 1_000_000
_LOG_FILE_BACKUPS = 3
_secrets: set[str] = set()


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def redact(text: str) -> str:
    """Replace every registered secret in ``text``."""
    for secret in _secrets:
        text = text.replace(secret, _REDACTED)
    return text


def register_secrets(*values: str) -> None:
    """Redact these values from every log record from now on."""
    _secrets.update(value for value in values if len(value) >= _MIN_SECRET_LENGTH)


def configure_logging(*, verbose: bool = False, log_file: Path | None = None) -> None:
    """Log to stderr, or only to ``log_file`` when given."""
    if log_file is None:
        handler: logging.Handler = logging.StreamHandler(sys.stderr)
    else:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_file, maxBytes=_LOG_FILE_MAX_BYTES, backupCount=_LOG_FILE_BACKUPS, encoding="utf-8"
        )
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # httpx logs every request URL at INFO level, Telegram token included.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
