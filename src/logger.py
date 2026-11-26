"""Structured logging with secret redaction and per-repository log files."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Iterable

_SECRETS: set[str] = set()
REDACTED = "***REDACTED***"


def register_secret(value: str | None) -> None:
    """Register a value that must never appear in any log line."""
    if value and len(value) >= 4:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        red = redact(msg)
        if red != msg:
            record.msg = red
            record.args = ()
        return True


_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(level: str = "INFO", secrets: Iterable[str | None] = ()) -> logging.Logger:
    for s in secrets:
        register_secret(s)
    root = logging.getLogger("diu")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(_RedactingFilter())
    root.addHandler(handler)
    root.propagate = False
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"diu.{name}")


def attach_repo_log(repo_name: str, log_dir: Path) -> logging.Handler:
    """Attach a per-repository file handler; caller must detach_repo_log() it."""
    log_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in repo_name)
    handler = logging.FileHandler(log_dir / f"{safe}.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(_RedactingFilter())
    logging.getLogger("diu").addHandler(handler)
    return handler


def detach_repo_log(handler: logging.Handler) -> None:
    logging.getLogger("diu").removeHandler(handler)
    handler.close()
