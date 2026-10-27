"""Local persistence and redacted, rotating logs for unattended operation."""

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets=()):
        super().__init__("%(asctime)s | %(levelname)s | %(message)s")
        self.secrets = tuple(value for value in secrets if value)

    def format(self, record):
        result = super().format(record)
        for secret in self.secrets:
            result = result.replace(secret, "[REDACTED]")
        return result


def setup_logging(base_dir, secrets=()):
    log_dir = Path(base_dir) / "logs"
    log_dir.mkdir(exist_ok=True)
    formatter = RedactingFormatter(secrets)
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(log_dir / "bot.log", maxBytes=1_000_000,
                            backupCount=3, encoding="utf-8"),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)
    # HTTP URLs contain the Telegram token; routine requests are not useful logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def save_json(path, value):
    """No await between write and replace: safe within the bot's event loop."""
    tmp_path = str(path) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
