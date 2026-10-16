"""知识库相关响应模型（M4）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DocumentPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    file_type: str
    size_bytes: int
    #: ready（已入库）/ failed（解析失败，原因见 error_message）
    status: str
    chunk_count: int
    char_count: int
    error_message: str = ""
    created_at: datetime
    #: 该文档是否已有向量（用于前端展示索引状态）
    embedded: bool = False


class DocumentListResponse(BaseModel):
    items: list[DocumentPublic]
    total: int


class ChunkPublic(BaseModel):
    id: str
    document_id: str | None = None
    seq: int
    content: str
    char_count: int
    #: 是否已生成向量（接入本地 BGE 模型后为 True）
    has_embedding: bool = False


class ChunkListResponse(BaseModel):
    items: list[ChunkPublic]
    total: int
    limit: int = 50
    offset: int = 0


class ReindexResponse(BaseModel):
    """重建索引（补齐 / 重算文本块向量）结果。"""

    #: 本次需要处理的片段总数
    total: int
    #: 实际写入向量的片段数
    embedded: int
    #: 是否全量重算
    forced: bool = False
    #: 使用的嵌入后端名，例如 hash / local:BAAI/bge-small-zh-v1.5
    backend: str


class SearchHitPublic(BaseModel):
    """一条检索命中（与 `knowledge_search` 工具使用同一套检索实现）。"""

    document_id: str | None = None
    filename: str
    seq: int
    score: float
    content: str


class SearchResponse(BaseModel):
    query: str
    backend: str
    items: list[SearchHitPublic]


__all__ = [
    "ChunkListResponse",
    "ChunkPublic",
    "DocumentListResponse",
    "DocumentPublic",
    "ReindexResponse",
    "SearchHitPublic",
    "SearchResponse",
]
