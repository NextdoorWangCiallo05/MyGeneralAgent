"""文档解析（M4）：把上传的文件转成纯文本，供分块与后续向量化使用。

支持的类型：

- `.txt` / `.md` / `.markdown`（以及 `.log` / `.csv` / `.json`）：按 UTF-8 解码，失败回退 GB18030；
- `.pdf`：依赖可选包 `pypdf`，未安装或解析失败时给出明确错误提示。

> 结构化文本（csv / json）同样按纯文本读取，交给模型自己理解结构，不做额外解析。

解析失败统一抛 `app.core.errors` 中的业务异常，由 API 层转成 JSON 错误响应。
"""

from __future__ import annotations

import io
import logging
from pathlib import PurePosixPath

from app.core.errors import DocumentParseError, UnsupportedDocumentError

logger = logging.getLogger("app.document")

#: 直接按文本解码的类型（csv / json 这类结构化文本同样按文本读取，与可上传白名单保持一致）
TEXT_TYPES = {"txt", "md", "markdown", "text", "log", "csv", "json"}
#: 需要 pypdf 的类型
PDF_TYPES = {"pdf"}
#: 允许上传的全部类型（与 SUPPORTED_TYPES 保持同步）
SUPPORTED_TYPES = TEXT_TYPES | PDF_TYPES

#: 解码顺序：utf-8-sig 放最前，顺带吃掉 Windows 记事本 / Excel 导出写入的 BOM
_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")

#: 文件名入库上限（`attachments.filename` / `knowledge_documents.filename` 都是 VARCHAR(255)）
MAX_FILENAME_CHARS = 255


def detect_file_type(filename: str) -> str:
    """从文件名推断类型：小写扩展名，无扩展名时返回空串。"""
    return PurePosixPath(filename.replace("\\", "/")).suffix.lower().lstrip(".")


def ensure_filename_length(filename: str) -> str:
    """校验文件名长度，原样返回。

    超长时必须在**入库前**给出可读的错误：否则会在 INSERT 时抛
    `DataError (1406) Data too long for column 'filename'`，用户只看到 500。
    """
    if len(filename) > MAX_FILENAME_CHARS:
        raise DocumentParseError(f"文件名过长（最多 {MAX_FILENAME_CHARS} 个字符）")
    return filename


def parse_document(filename: str, data: bytes, *, file_type: str | None = None) -> str:
    """把文件内容解析为纯文本。"""
    kind = (file_type or detect_file_type(filename)).lower()
    if not data:
        raise DocumentParseError("文件内容为空")
    if kind in TEXT_TYPES:
        return _decode_text(data)
    if kind in PDF_TYPES:
        return _parse_pdf(data)
    raise UnsupportedDocumentError(f"暂不支持的文件类型：.{kind or '未知'}")


def _decode_text(data: bytes) -> str:
    for encoding in _TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentParseError("无法识别文件编码，请另存为 UTF-8 后重试")


def _parse_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 取决于部署环境是否安装可选依赖
        raise UnsupportedDocumentError(
            "解析 PDF 需要可选依赖 pypdf，请执行：pip install pypdf"
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 - pypdf 的异常类型不稳定
        raise DocumentParseError(f"PDF 解析失败：{exc}") from exc

    text = "\n\n".join(page.strip() for page in pages if page.strip())
    if not text:
        raise DocumentParseError("未能从 PDF 中提取到文本（可能是扫描件，需要 OCR）")
    return text


__all__ = [
    "MAX_FILENAME_CHARS",
    "PDF_TYPES",
    "SUPPORTED_TYPES",
    "TEXT_TYPES",
    "detect_file_type",
    "ensure_filename_length",
    "parse_document",
]
