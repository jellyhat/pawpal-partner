"""Logging setup shared by the app, CLI demo, and evaluation harness.

Logs go to ``logs/pawpal.log`` (rotating) and, at WARNING and above, to the
console. Every planner conflict, guardrail trigger, model call, and agent
tool call is recorded, so a run can be audited after the fact.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(os.environ.get("PAWPAL_LOG_DIR", Path(__file__).parent / "logs"))
LOG_FILE = LOG_DIR / "pawpal.log"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: int = logging.INFO, console: bool = True) -> logging.Logger:
    """Configure the ``pawpal`` logger once and return it."""
    logger = logging.getLogger("pawpal")
    if getattr(logger, "_pawpal_configured", False):
        return logger
    logger.setLevel(level)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=500_000, backupCount=3)
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setLevel(logging.WARNING)
        stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(stream)

    logger.propagate = False
    logger._pawpal_configured = True  # type: ignore[attr-defined]
    return logger


def read_recent_logs(lines: int = 50) -> str:
    """Return the last ``lines`` lines of the log file (for the UI)."""
    if not LOG_FILE.exists():
        return ""
    return "\n".join(LOG_FILE.read_text().splitlines()[-lines:])
