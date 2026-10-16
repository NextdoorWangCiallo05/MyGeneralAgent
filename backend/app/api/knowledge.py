"""知识库接口（M4）：文档上传 / 列表 / 分块预览 / 删除 / 重建索引 / 检索调试。

上传即完成「解析 → 分块 → 向量化 → 入库」，文本块写入 `embeddings` 表。
嵌入后端见 `app/core/embedding.py`：默认 `hash`（零依赖），可切换为本地 BGE 语义模型。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Query, UploadFile, status

from app.api.deps import CurrentUser, Store, rate_limited
from app.config import get_settings
from app.core.chunking import split_text
from app.core.document_parser import detect_file_type, ensure_filename_length, parse_document
from app.core.embedding import Embedder, get_embedder
from app.core.errors import (
    DocumentNotFoundError,
    DocumentParseError,
    DocumentTooLargeError,
    EmbeddingUnavailableError,
    UnsupportedDocumentError,
)
from app.core.retriever import search_knowledge
from app.schemas.auth import MessageResponse
from app.schemas.knowledge import (
    ChunkListResponse,
    ChunkPublic,
    DocumentListResponse,
    DocumentPublic,
    ReindexResponse,
    SearchHitPublic,
    SearchResponse,
)
from app.store.entities import KnowledgeDocument, User

logger = logging.getLogger("app.api.knowledge")

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

#: 重建索引时每批处理的文本块数量
_REINDEX_BATCH = 64


def _allowed_types() -> set[str]:
    raw = get_settings().knowledge_allowed_type_list
    return {item.strip().lower() for item in raw if item.strip()}


async def _get_owned_document(
    document_id: str, user: User, store: Store
) -> KnowledgeDocument:
    document = await store.get_document(document_id)
    if document is None or document.user_id != user.id:
        raise DocumentNotFoundError()
    return document


def _require_embedder() -> Embedder:
    """获取嵌入后端；未启用向量化时明确报错。"""
    embedder = get_embedder()
    if embedder is None:
        raise EmbeddingUnavailableError()
    return embedder


async def _embed_texts(texts: list[str]) -> list[list[float]] | None:
    """向量化文本；未启用向量化或编码失败时返回 None（文档照常入库，可稍后重建索引）。"""
    embedder = get_embedder()
    if embedder is None or not texts:
        return None
    try:
        return await embedder.encode(texts)
    except Exception:  # noqa: BLE001 - 向量化失败不应导致上传失败
        logger.exception("文档向量化失败，可稍后通过「重建索引」补齐")
        return None


@router.post(
    "/documents",
    response_model=DocumentPublic,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limited("upload"))],
)
async def upload_document(
    user: CurrentUser,
    store: Store,
    file: UploadFile = File(...),
) -> DocumentPublic:
    """上传文档：解析为文本后分块入库。"""
    settings = get_settings()
    filename = ensure_filename_length((file.filename or "").strip() or "未命名文档")
    file_type = detect_file_type(filename)

    allowed = _allowed_types()
    if allowed and file_type not in allowed:
        raise UnsupportedDocumentError(f"仅支持 {'、'.join(sorted(allowed))} 类型的文档")

    data = await file.read()
    if not data:
        raise DocumentParseError("文件内容为空")
    if len(data) > settings.knowledge_max_file_bytes:
        limit_mb = settings.knowledge_max_file_bytes / (1024 * 1024)
        raise DocumentTooLargeError(f"文档不能超过 {limit_mb:g} MB")

    text = parse_document(filename, data, file_type=file_type)
    chunks = split_text(
        text,
        chunk_size=settings.knowledge_chunk_size,
        overlap=settings.knowledge_chunk_overlap,
    )
    if not chunks:
        raise DocumentParseError("文档解析后没有可索引的文本内容")

    document = await store.create_document(
        user_id=user.id,
        filename=filename,
        file_type=file_type,
        size_bytes=len(data),
    )
    embeddings = await _embed_texts(chunks)
    written = await store.add_chunks(
        user_id=user.id,
        document_id=document.id,
        contents=chunks,
        embeddings=embeddings,
    )
    document = await store.update_document(
        document,
        chunk_count=written,
        char_count=len(text),
        status="ready",
    )
    return DocumentPublic.model_validate(document).model_copy(
        update={"embedded": bool(embeddings)}
    )


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(user: CurrentUser, store: Store) -> DocumentListResponse:
    settings = get_settings()
    documents = await store.list_documents(user.id)
    # 一次取出已向量化的片段，用于标记每份文档的索引状态
    embedded = await store.list_chunks(
        user.id,
        source="knowledge",
        only_embedded=True,
        limit=settings.knowledge_search_max_chunks,
    )
    indexed = {chunk.document_id for chunk in embedded}
    items = [
        DocumentPublic.model_validate(document).model_copy(
            update={"embedded": document.id in indexed}
        )
        for document in documents
    ]
    return DocumentListResponse(items=items, total=len(items))


@router.get("/documents/{document_id}/chunks", response_model=ChunkListResponse)
async def list_chunks(
    document_id: str,
    user: CurrentUser,
    store: Store,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ChunkListResponse:
    """分块预览：便于上传后核对切分效果。"""
    document = await _get_owned_document(document_id, user, store)
    chunks = await store.list_chunks(
        user.id, document_id=document_id, limit=limit, offset=offset
    )
    items = [
        ChunkPublic(
            id=chunk.id,
            document_id=chunk.document_id,
            seq=chunk.seq,
            content=chunk.content,
            char_count=len(chunk.content),
            has_embedding=chunk.embedding is not None,
        )
        for chunk in chunks
    ]
    return ChunkListResponse(
        items=items, total=document.chunk_count, limit=limit, offset=offset
    )


@router.delete("/documents/{document_id}", response_model=MessageResponse)
async def delete_document(
    document_id: str, user: CurrentUser, store: Store
) -> MessageResponse:
    document = await _get_owned_document(document_id, user, store)
    await store.delete_document(document)
    return MessageResponse(message="文档已删除")


@router.post(
    "/reindex",
    response_model=ReindexResponse,
    dependencies=[Depends(rate_limited("upload"))],
)
async def reindex_documents(
    user: CurrentUser,
    store: Store,
    force: bool = Query(default=False, description="true 时重算全部片段（切换嵌入后端后使用）"),
) -> ReindexResponse:
    """补齐文本块向量；`force=true` 时全量重算。

    默认只处理尚未向量化的片段（例如上传时嵌入后端不可用、或关闭向量化后补开）；
    切换 `EMBEDDING_BACKEND` 后旧向量与新查询向量不在同一空间，必须 `force=true` 重建。
    """
    embedder = _require_embedder()
    total_chunks = await store.count_chunks(user.id, source="knowledge")
    # 默认只补齐缺失向量，因此待处理数 = 总块数 − 已向量化块数
    pending_total = (
        total_chunks
        if force
        else total_chunks
        - await store.count_chunks(user.id, source="knowledge", only_embedded=True)
    )

    embedded = 0
    cursor = 0
    # 始终按完整列表分页，再自行挑出待处理片段：
    # 一是 offset 分页在筛选结果上会跳块，二是写入向量后筛选条件会随之变化。
    while True:
        batch = await store.list_chunks(
            user.id, source="knowledge", limit=_REINDEX_BATCH, offset=cursor
        )
        if not batch:
            break
        cursor += len(batch)
        pending = [chunk for chunk in batch if force or not chunk.embedding]
        if not pending:
            continue
        vectors = await embedder.encode([chunk.content for chunk in pending])
        embedded += await store.update_chunk_embeddings(
            user_id=user.id,
            items=[
                (chunk.id, vector)
                for chunk, vector in zip(pending, vectors)
                if vector
            ],
        )
    return ReindexResponse(
        total=pending_total, embedded=embedded, forced=force, backend=embedder.name
    )


@router.get("/search", response_model=SearchResponse)
async def search_documents(
    user: CurrentUser,
    store: Store,
    q: str = Query(min_length=1, max_length=500, description="检索问句"),
    top_k: int = Query(default=0, ge=0, le=50, description="返回片段数，0 表示使用默认值"),
) -> SearchResponse:
    """检索调试接口：与 `knowledge_search` 工具共用同一实现，便于核对召回效果。"""
    settings = get_settings()
    embedder = _require_embedder()
    hits = await search_knowledge(
        store,
        user.id,
        q,
        top_k=top_k or None,
        embedder=embedder,
        settings=settings,
    )
    return SearchResponse(
        query=q,
        backend=embedder.name,
        items=[
            SearchHitPublic(
                document_id=hit.document_id,
                filename=hit.filename,
                seq=hit.seq,
                score=round(hit.score, 4),
                content=hit.content,
            )
            for hit in hits
        ],
    )
