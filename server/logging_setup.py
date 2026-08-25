"""Structured logging: key=value lines to stdout plus a rotating file in the
data directory."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import get_settings

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging() -> logging.Logger:
    settings = get_settings()
    settings.logs_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("nomos")
    if root.handlers:  # already configured (uvicorn reload / tests)
        return root
    root.setLevel(logging.INFO)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(stream)

    file_handler = RotatingFileHandler(
        settings.logs_dir / "nomos.log", maxBytes=5 * 1024 * 1024, backupCount=3
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)
    return root


def kv(**fields: object) -> str:
    """Render fields as a stable key=value log suffix."""
    return " ".join(f"{k}={v}" for k, v in fields.items())
