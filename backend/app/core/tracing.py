"""可观测性：把一轮对话里的模型调用与工具执行串成一条链路（M5）。

《技术设计文档》10. 可观测性要求「接入 Langfuse / LangSmith，记录每次 LLM 调用、
工具执行、耗时与 token 用量」。这里的实现原则：

- **默认零依赖、零副作用**：未配置 `LANGFUSE_PUBLIC_KEY/SECRET_KEY` 或未安装
  `langfuse` SDK 时，自动降级为 no-op，业务代码不需要任何分支判断；
- **fail-open**：任何埋点异常都不会影响对话主流程；
- 未接 Langfuse 时可用 `OBSERVABILITY_LOG_SPANS=true` 把链路埋点输出成结构化日志，
  便于本地排查与验收。

链路结构：`turn`（一轮对话）→ `llm.chat`（每次模型调用）/ `tool.call`（每次工具执行）。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Settings, get_settings
from app.core.logging import set_trace_id

logger = logging.getLogger("app.tracing")

#: 单条埋点里输入/输出文本的最大长度，避免把超长上下文整段送进观测系统
MAX_TEXT = 2000


def _clip(value: Any, limit: int = MAX_TEXT) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return f"{value[:limit]}…(已截断，共 {len(value)} 字符)"
    return value


@dataclass
class Span:
    """一次操作的埋点，同时支持 `with` 与手动 `end()` 两种用法。"""

    name: str
    input: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    status: str = "ok"
    error: str = ""
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    _turn: "Turn | None" = None
    _ended: bool = False

    @property
    def duration_ms(self) -> int:
        end = self.ended_at if self.ended_at is not None else time.monotonic()
        return int((end - self.started_at) * 1000)

    def update(self, *, output: Any = None, status: str | None = None, **metadata: Any) -> None:
        """补充结果（不结束埋点）。"""
        if output is not None:
            self.output = output
        if status:
            self.status = status
        if metadata:
            self.metadata.update(metadata)

    def end(
        self,
        *,
        status: str | None = None,
        output: Any = None,
        error: str = "",
        **metadata: Any,
    ) -> None:
        self.update(output=output, status=status, **metadata)
        if error:
            self.error = error
            if self.status == "ok":
                self.status = "error"
        if self._ended:
            return
        self._ended = True
        self.ended_at = time.monotonic()
        if self._turn is not None:
            self._turn._emit_span(self)  # noqa: SLF001 - 同一个模块内的协作对象

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc is not None:
            self.end(status="error", error=f"{exc_type.__name__}: {exc}")
        else:
            self.end()
        return False


@dataclass
class Turn:
    """一轮对话的链路根节点。始终可用（没有 sink 时所有方法都是空操作）。"""

    name: str
    trace_id: str
    user_id: str = ""
    session_id: str = ""
    input: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    status: str = "ok"
    error: str = ""
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    spans: list[Span] = field(default_factory=list)
    _sinks: list["TracerSink"] = field(default_factory=list)
    _finished: bool = False

    @property
    def duration_ms(self) -> int:
        end = self.ended_at if self.ended_at is not None else time.monotonic()
        return int((end - self.started_at) * 1000)

    @property
    def enabled(self) -> bool:
        return bool(self._sinks)

    def span(self, name: str, *, input: Any = None, **metadata: Any) -> Span:
        span = Span(name=name, input=input, metadata=metadata, _turn=self)
        self.spans.append(span)
        return span

    def end(self, *, output: Any = None, status: str = "ok", error: str = "", **metadata: Any) -> None:
        if self._finished:
            return
        self._finished = True
        self.ended_at = time.monotonic()
        if output is not None:
            self.output = output
        self.status = status
        self.error = error
        if metadata:
            self.metadata.update(metadata)
        for sink in self._sinks:
            _safe(sink, "turn_ended", self)

    # ------------------------------------------------------------- 内部
    def _emit_span(self, span: Span) -> None:
        for sink in self._sinks:
            _safe(sink, "span_ended", self, span)

    def _opened(self) -> None:
        for sink in self._sinks:
            _safe(sink, "turn_started", self)


def _safe(sink: "TracerSink", method: str, *args: Any) -> None:
    """埋点失败绝不影响业务（fail-open）。"""
    try:
        getattr(sink, method)(*args)
    except Exception:  # noqa: BLE001
        logger.warning("观测埋点失败: sink=%s method=%s", getattr(sink, "name", "?"), method, exc_info=True)


class TracerSink(Protocol):
    name: str

    def turn_started(self, turn: Turn) -> None: ...

    def span_ended(self, turn: Turn, span: Span) -> None: ...

    def turn_ended(self, turn: Turn) -> None: ...

    def flush(self) -> None: ...


class LogSink:
    """把链路埋点写成结构化日志（`OBSERVABILITY_LOG_SPANS=true` 时启用）。"""

    name = "log"

    def turn_started(self, turn: Turn) -> None:
        logger.info(
            "trace turn start: %s",
            turn.name,
            extra={
                "session_id": turn.session_id,
                "user_id": turn.user_id,
                "status": "started",
            },
        )

    def span_ended(self, turn: Turn, span: Span) -> None:
        logger.info(
            "trace span: %s",
            span.name,
            extra={
                "session_id": turn.session_id,
                "user_id": turn.user_id,
                "tool": span.metadata.get("tool", ""),
                "duration_ms": span.duration_ms,
                "status": span.status,
                "model": span.metadata.get("model", ""),
                "prompt_tokens": span.metadata.get("prompt_tokens"),
                "completion_tokens": span.metadata.get("completion_tokens"),
            },
        )

    def turn_ended(self, turn: Turn) -> None:
        logger.info(
            "trace turn end: %s",
            turn.name,
            extra={
                "session_id": turn.session_id,
                "user_id": turn.user_id,
                "duration_ms": turn.duration_ms,
                "status": turn.status,
                "steps": len(turn.spans),
            },
        )

    def flush(self) -> None:  # 日志即时写出，无需 flush
        return None


class LangfuseSink:
    """把链路送到 Langfuse（v3 SDK 的 OTEL 风格 API）。"""

    name = "langfuse"

    def __init__(self, client: Any) -> None:
        self._client = client
        self._roots: dict[int, Any] = {}

    # ------------------------------------------------------------- 生命周期
    def turn_started(self, turn: Turn) -> None:
        manager = self._client.start_as_current_span(
            name=turn.name,
            input=_clip(turn.input),
            metadata={"session_id": turn.session_id, **turn.metadata},
            end_on_exit=False,
        )
        root = manager.__enter__()
        self._roots[id(turn)] = (manager, root)
        updater = getattr(self._client, "update_current_trace", None)
        if callable(updater):
            updater(user_id=turn.user_id or None, session_id=turn.session_id or None, metadata=turn.metadata)

    def span_ended(self, turn: Turn, span: Span) -> None:
        manager = self._client.start_as_current_span(
            name=span.name,
            input=_clip(span.input),
            metadata={**span.metadata, "duration_ms": span.duration_ms},
            end_on_exit=False,
        )
        child = manager.__enter__()
        _update_span(
            child,
            output=_clip(span.output),
            level="ERROR" if span.status not in {"ok", "success"} else "DEFAULT",
            status_message=span.error or None,
        )
        manager.__exit__(None, None, None)

    def turn_ended(self, turn: Turn) -> None:
        entry = self._roots.pop(id(turn), None)
        if entry is None:
            return
        manager, root = entry
        _update_span(
            root,
            output=_clip(turn.output),
            level="ERROR" if turn.status not in {"ok", "success"} else "DEFAULT",
            status_message=turn.error or None,
        )
        manager.__exit__(None, None, None)

    def flush(self) -> None:
        flusher = getattr(self._client, "flush", None)
        if callable(flusher):
            flusher()


def _update_span(handle: Any, **fields: Any) -> None:
    updater = getattr(handle, "update", None)
    if callable(updater):
        updater(**{k: v for k, v in fields.items() if v is not None})


class Tracer:
    """链路入口：创建 turn，并把埋点扇出到各个 sink。"""

    def __init__(self, sinks: list[TracerSink] | None = None) -> None:
        self._sinks = list(sinks or [])

    @property
    def enabled(self) -> bool:
        return bool(self._sinks)

    @property
    def sink_names(self) -> list[str]:
        return [sink.name for sink in self._sinks]

    def start_turn(
        self,
        *,
        name: str = "chat.turn",
        user_id: str = "",
        session_id: str = "",
        input: Any = None,
        **metadata: Any,
    ) -> Turn:
        turn = Turn(
            name=name,
            trace_id=uuid.uuid4().hex[:16],
            user_id=user_id,
            session_id=session_id,
            input=input,
            metadata=metadata,
            _sinks=self._sinks,
        )
        if self._sinks:
            # 后续日志都带上 trace_id，便于把日志与链路对起来
            set_trace_id(turn.trace_id)
            turn._opened()  # noqa: SLF001
        return turn

    def flush(self) -> None:
        for sink in self._sinks:
            _safe(sink, "flush")


def build_tracer(settings: Settings | None = None) -> Tracer:
    """按配置组装 Tracer；不满足条件时返回空 Tracer（no-op）。"""
    settings = settings or get_settings()
    sinks: list[TracerSink] = []

    if settings.observability_log_spans:
        sinks.append(LogSink())

    if settings.langfuse_enabled:
        try:
            from langfuse import Langfuse  # 延迟导入：没装 SDK 也能跑
        except Exception:  # noqa: BLE001 - 缺依赖 / 依赖损坏都只降级
            logger.warning(
                "已配置 LANGFUSE_*，但 langfuse SDK 不可用，链路追踪降级为 no-op"
                "（pip install -r requirements.txt 后重启）"
            )
        else:
            try:
                client = Langfuse(
                    public_key=settings.langfuse_public_key,
                    secret_key=settings.langfuse_secret_key,
                    host=settings.langfuse_host,
                )
            except Exception:  # noqa: BLE001
                logger.warning("Langfuse 客户端初始化失败，链路追踪降级为 no-op", exc_info=True)
            else:
                sinks.append(LangfuseSink(client))
                logger.info("Langfuse 链路追踪已启用: host=%s", settings.langfuse_host)

    return Tracer(sinks)


_tracer: Tracer | None = None


def get_tracer() -> Tracer:
    global _tracer
    if _tracer is None:
        _tracer = build_tracer()
    return _tracer


def reset_tracer(tracer: Tracer | None = None) -> None:
    """替换（或清空）全局 Tracer，供测试使用；无参数时下次获取会按配置重建。"""
    global _tracer
    _tracer = tracer


def flush_tracer() -> None:
    try:
        get_tracer().flush()
    except Exception:  # noqa: BLE001
        logger.debug("tracer flush 失败", exc_info=True)
