"""统一业务异常与错误码。

错误码与《技术设计文档》6.3 节保持一致，API 层统一转换为 JSON：
    { "code": "UNAUTHORIZED", "message": "..." }
"""

from __future__ import annotations

from typing import Any, Mapping


class AppError(Exception):
    """业务异常基类。"""

    code: str = "INTERNAL_ERROR"
    http_status: int = 400
    default_message: str = "请求处理失败"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        http_status: int | None = None,
        details: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.message = message or self.default_message
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status
        self.details = details
        #: 附加响应头（如限流的 Retry-After / X-RateLimit-*），由异常处理器透出
        self.headers = dict(headers or {})
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    http_status = 401
    default_message = "未认证或登录已失效"


class ForbiddenError(AppError):
    code = "FORBIDDEN"
    http_status = 403
    default_message = "无权限访问该资源"


class InvalidCredentialsError(AppError):
    code = "INVALID_CREDENTIALS"
    http_status = 401
    default_message = "用户名或密码错误"


class UserExistsError(AppError):
    code = "USER_EXISTS"
    http_status = 409
    default_message = "用户名或邮箱已被注册"


class InvalidResetTokenError(AppError):
    code = "INVALID_RESET_TOKEN"
    http_status = 400
    default_message = "重置令牌无效、已过期或已被使用"


class SessionNotFoundError(AppError):
    code = "SESSION_NOT_FOUND"
    http_status = 404
    default_message = "会话不存在"


class ConfirmationNotFoundError(AppError):
    code = "CONFIRMATION_NOT_FOUND"
    http_status = 409
    default_message = "待确认的工具调用不存在或已结束（可能已超时）"


class RateLimitedError(AppError):
    code = "RATE_LIMITED"
    http_status = 429
    default_message = "请求过于频繁，请稍后再试"


class ApiKeyNotConfiguredError(AppError):
    code = "API_KEY_NOT_CONFIGURED"
    http_status = 503
    default_message = "管理员尚未配置模型 API-Key"


class LLMError(AppError):
    code = "LLM_ERROR"
    http_status = 502
    default_message = "模型调用失败"


class DocumentNotFoundError(AppError):
    code = "DOCUMENT_NOT_FOUND"
    http_status = 404
    default_message = "文档不存在"


class UnsupportedDocumentError(AppError):
    code = "UNSUPPORTED_DOCUMENT"
    http_status = 400
    default_message = "不支持的文件类型"


class DocumentParseError(AppError):
    code = "DOCUMENT_PARSE_ERROR"
    http_status = 400
    default_message = "文档解析失败"


class DocumentTooLargeError(AppError):
    code = "DOCUMENT_TOO_LARGE"
    http_status = 413
    default_message = "文档超过大小限制"


class AttachmentNotFoundError(AppError):
    code = "ATTACHMENT_NOT_FOUND"
    http_status = 404
    default_message = "附件不存在或已被清理"


class TooManyAttachmentsError(AppError):
    code = "TOO_MANY_ATTACHMENTS"
    http_status = 400
    default_message = "单条消息携带的附件数量超出上限"


class EmbeddingUnavailableError(AppError):
    code = "EMBEDDING_UNAVAILABLE"
    http_status = 400
    default_message = "当前未启用向量化（EMBEDDING_BACKEND=none），无法进行知识库检索"
