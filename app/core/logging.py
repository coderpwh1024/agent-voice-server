"""Consistent text/JSON service logging without third-party payload dumps."""

import json
import logging
import traceback
from datetime import UTC, datetime

from app.core.settings import LogLevel


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str | list[str]] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            payload["error_type"] = record.exc_info[0].__name__
            payload["traceback"] = [
                f"{frame.filename}:{frame.lineno} in {frame.name}"
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(payload, ensure_ascii=False)


class ServiceLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(
            ("mem0", "httpx", "httpcore", "openai", "ddgs", "primp", "mcp.")
        )


def configure_logging(level: LogLevel, log_format: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter()
        if log_format == "json"
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(ServiceLogFilter())
    logging.basicConfig(level=level.to_logging_level(), handlers=[handler], force=True)
