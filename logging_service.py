"""File-based JSON logging for bot-backend.

Every log line is a single JSON object appended to a size-rotated file
(default ``logs/bot-backend.log``) and echoed to stdout. Each line carries a
``kind`` so the file can be filtered with ``jq``:

    http_request / http_response / http_error   inbound Flask traffic
    webhook_event                               POST /events from the gateway
    outbound_request / outbound_response        calls to the gateway /actions/*

    tail -f logs/bot-backend.log | jq 'select(.kind == "webhook_event")'

Lines logged inside a Flask request automatically get that request's
``request_id`` (taken from ``X-Request-ID`` or generated), which is also
echoed back in the response header so a request can be traced end to end.

Env vars: LOG_DIR (default ./logs), LOG_FILE (bot-backend.log),
LOG_LEVEL (INFO), LOG_MAX_BYTES (10 MB), LOG_BACKUP_COUNT (5),
LOG_BODY_MAX_CHARS (4000).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

from flask import Flask, g, has_request_context, jsonify, request
from werkzeug.exceptions import HTTPException

LOG_DIR = os.getenv("LOG_DIR", os.path.join(os.path.dirname(__file__), "logs"))
LOG_FILE = os.getenv("LOG_FILE", "bot-backend.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))
BODY_MAX_CHARS = int(os.getenv("LOG_BODY_MAX_CHARS", "4000"))

# Header / body keys whose values never reach the log file.
SENSITIVE_KEYS = frozenset({
    "authorization", "cookie", "set-cookie", "x-internal-key", "x-api-key",
    "api_key", "apikey", "access_token", "token", "password", "secret",
})

# LogRecord attributes set by the stdlib; anything else came in via `extra=`.
_STANDARD_FIELDS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def redact(value: Any) -> Any:
    """Recursively mask sensitive keys in dicts / lists."""
    if isinstance(value, dict):
        return {k: "***" if str(k).lower() in SENSITIVE_KEYS else redact(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def truncate(value: Any, limit: int = BODY_MAX_CHARS) -> Any:
    """Keep large bodies from bloating the log: oversize values become a clipped string."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    if len(text) <= limit:
        return value
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


def body_for_log(value: Any) -> Any:
    return truncate(redact(value))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc)
                .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class RequestIdFilter(logging.Filter):
    """Stamp the current Flask request id onto every record logged in a request."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id") and has_request_context():
            record.request_id = getattr(g, "request_id", None)
        return True


_configured = False


def configure_logging() -> None:
    """Attach the rotating JSON file handler (+ stdout) to the root logger. Idempotent."""
    global _configured
    if _configured:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    formatter, request_filter = JsonFormatter(), RequestIdFilter()

    file_handler = RotatingFileHandler(os.path.join(LOG_DIR, LOG_FILE), maxBytes=LOG_MAX_BYTES,
                                       backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    console_handler = logging.StreamHandler(sys.stdout)
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in (file_handler, console_handler):
        handler.setFormatter(formatter)
        handler.addFilter(request_filter)
        root.addHandler(handler)
    # Werkzeug's own access line duplicates http_response; keep only its warnings.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


http_log = get_logger("bot_backend.http")
webhook_log = get_logger("bot_backend.webhook")


def _request_body() -> Any:
    if request.is_json:
        return request.get_json(silent=True)
    data = request.get_data(cache=True, as_text=True)
    return data or None


def _response_body(response) -> Any:
    if response.direct_passthrough or response.is_streamed:
        return "<streamed>"
    if response.is_json:
        return response.get_json(silent=True)
    return response.get_data(as_text=True) or None


def init_app(app: Flask) -> None:
    """Log every inbound request, its response, and any unhandled exception."""
    configure_logging()

    @app.before_request
    def _log_request():
        g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        g.request_started = time.perf_counter()
        if request.method == "OPTIONS":  # CORS preflight noise
            return
        http_log.info("%s %s", request.method, request.path, extra={
            "kind": "http_request",
            "method": request.method,
            "path": request.path,
            "query": request.args.to_dict(flat=False) or None,
            "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
            "user_agent": request.user_agent.string or None,
            "headers": redact(dict(request.headers)),
            "body": body_for_log(_request_body()),
        })

    @app.after_request
    def _log_response(response):
        request_id = getattr(g, "request_id", None)
        if request_id:
            response.headers["X-Request-ID"] = request_id
        if request.method == "OPTIONS":
            return response
        started = getattr(g, "request_started", None)
        duration_ms = round((time.perf_counter() - started) * 1000, 2) if started else None
        level = (logging.ERROR if response.status_code >= 500
                 else logging.WARNING if response.status_code >= 400 else logging.INFO)
        http_log.log(level, "%s %s -> %s", request.method, request.path, response.status_code,
                     extra={
                         "kind": "http_response",
                         "method": request.method,
                         "path": request.path,
                         "endpoint": request.endpoint,
                         "status": response.status_code,
                         "duration_ms": duration_ms,
                         "body": body_for_log(_response_body(response)),
                     })
        return response

    @app.errorhandler(Exception)
    def _log_exception(exc):
        # More specific errorhandlers (ValueError, ...) still win; HTTP errors
        # like 404/405 pass through and are logged by _log_response.
        if isinstance(exc, HTTPException):
            return exc
        http_log.error("Unhandled exception on %s %s", request.method, request.path,
                       exc_info=exc, extra={"kind": "http_error",
                                            "method": request.method, "path": request.path})
        return jsonify({"error": "internal server error",
                        "request_id": getattr(g, "request_id", None)}), 500
