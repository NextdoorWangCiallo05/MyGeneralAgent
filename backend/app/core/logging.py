"""结构化日志与请求上下文（M5）。

目标（《技术设计文档》10. 可观测性：日志规范）：

- 同一次 HTTP 请求链路上的所有日志都带同一个 `request_id`，便于检索与排障；
- `trace_id` 与 Langfuse 的链路 ID 打通（见 `app.core.tracing`）；
- 生产可用 `LOG_FORMAT=json` 输出单行 JSON，直接对接日志采集；
- 业务字段（session_id / tool / duration_ms …）通过 `logging` 的 `extra=` 传入，
  JSON 模式下会原样落进日志，开发模式的 text 格式则忽略这些附加字段。
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

#: 允许出现在日志里的结构化字段白名单，避免把任意对象塞进日志
EXTRA_FIELDS = (
    "user_id",
    "session_id",
    "tool",
    "tool_call_id",
    "status",
    "duration_ms",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "steps",
    "method",
    "path",
    "http_status",
    "ip",
    "scope",
    "limit",
    "retry_after",
    "backend",
)

_request_id: ContextVar[str] = ContextVar("request_id", default="")
_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def new_request_id() -> str:
    """生成短请求 ID（32 位十六进制太吵，链路内唯一即可）。"""
    return uuid.uuid4().hex[:16]


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str:
    return _request_id.get()


def set_trace_id(value: str) -> None:
    _trace_id.set(value)


def get_trace_id() -> str:
    return _trace_id.get()


class RequestContextFilter(logging.Filter):
    """把 contextvar 里的 request/trace 标识注入每条日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", ""):
            record.request_id = get_request_id()
        if not getattr(record, "trace_id", ""):
            record.trace_id = get_trace_id()
        return True


class JsonFormatter(logging.Formatter):
    """单行 JSON，便于 ELK / Loki 等系统按字段检索。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", "")
        if request_id:
            payload["request_id"] = request_id
        trace_id = getattr(record, "trace_id", "")
        if trace_id:
            payload["trace_id"] = trace_id

        for field in EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value not in (None, ""):
                payload[field] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """开发模式：人类可读，有 request_id 时加在方括号里。"""

    def format(self, record: logging.LogRecord) -> str:
        request_id = getattr(record, "request_id", "")
        prefix = f"[{request_id}] " if request_id else ""
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} [{record.name}] {prefix}"
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return base + message


def build_formatter(fmt: str) -> logging.Formatter:
    return JsonFormatter() if fmt.lower() == "json" else TextFormatter()


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    """配置根日志：统一格式，并让 uvicorn 的日志走同一套 handler。"""
    formatter = build_formatter(fmt)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(RequestContextFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn 默认自己装 handler，这里改为向根日志传播，保证格式与 request_id 一致
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
