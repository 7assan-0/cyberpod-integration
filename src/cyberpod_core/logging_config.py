import json
import logging
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    def format(self, record):
        payload = {"time": datetime.now(timezone.utc).isoformat(),
                   "level": record.levelname, "event": record.getMessage()}
        for key in ("request_id", "method", "route", "http_status", "session_id", "generation",
                    "status", "action", "error_code", "error_type", "event_type", "compensated",
                    "client", "duration_ms"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("cyberpod")
    logger.setLevel(logging.INFO)
    logger.handlers[:] = [handler]
    logger.propagate = False
    audit = logging.getLogger("cyberpod.audit")
    audit.setLevel(logging.INFO)
    audit.handlers[:] = [handler]
    audit.propagate = False
