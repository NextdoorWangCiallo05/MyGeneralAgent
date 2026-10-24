"""文本向量化（M4）：可插拔的嵌入后端。

| `EMBEDDING_BACKEND` | 实现 | 依赖 | 说明 |
|---------------------|------|------|------|
| `hash`（默认） | `HashEmbedder` | 无 | 字符 n-gram 哈希向量（blake2b），零依赖即可跑通「上传 → 向量化 → 检索」全链路 |
| `local` | `LocalEmbedder` | sentence-transformers + torch | 本地 BGE 中文模型，真正的语义检索 |
| `none` | — | — | 关闭向量化，`knowledge_search` 不再下发给模型 |

为什么默认 `hash`：本项目的默认运行环境不强依赖 torch（体积数 GB）。
`hash` 用 hashing trick 把字符 2/3-gram 映射到固定维度并做 L2 归一化，
于是「余弦相似度」等价于归一化后的字符 n-gram 重合度——中文无需分词也能命中大部分字面表达，
但**不具备语义泛化能力**。执行 `pip install -r requirements-ml.txt` 后把 `.env` 改为
`EMBEDDING_BACKEND=local`，再到知识库页面点「重建索引」即可升级为语义检索
（不同后端的向量不可混用，切换后必须重建）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from typing import Any, Protocol, Sequence

from app.config import Settings, get_settings
from app.core.errors import EmbeddingUnavailableError

logger = logging.getLogger("app.embedding")


class Embedder(Protocol):
    """嵌入后端统一契约。"""

    name: str
    dim: int

    async def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [value / norm for value in vector]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """余弦相似度；维度不一致或存在零向量时返回 0.0。"""
    if not left or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))


#: 哈希向量是纯 CPU 计算：批量足够大时丢到线程池，避免阻塞事件循环
_HASH_THREAD_THRESHOLD = 64

_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None
_semaphore_limit = 0


def _embedding_semaphore() -> asyncio.Semaphore:
    """进程内向量化并发闸门（按事件循环缓存）。

    本地模型推理会吃满 CPU，上传/重建索引/检索同时触发时若不限流，
    `to_thread` 的线程会互相抢占，延迟反而更高。上限由 `EMBEDDING_MAX_CONCURRENCY` 控制。
    """
    global _semaphore, _semaphore_loop, _semaphore_limit
    loop = asyncio.get_running_loop()
    limit = max(1, int(get_settings().embedding_max_concurrency))
    if _semaphore is None or _semaphore_loop is not loop or _semaphore_limit != limit:
        _semaphore = asyncio.Semaphore(limit)
        _semaphore_loop = loop
        _semaphore_limit = limit
    return _semaphore


def _grams(text: str) -> list[str]:
    """字符 2-gram 与 3-gram（中文无需分词，字符 n-gram 已能覆盖字面匹配）。"""
    compact = " ".join(text.split()).lower()
    grams: list[str] = []
    for size in (2, 3):
        if len(compact) < size:
            continue
        grams.extend(compact[index : index + size] for index in range(len(compact) - size + 1))
    if not grams and compact:
        grams.append(compact)
    return grams


class HashEmbedder:
    """字符 n-gram 哈希向量（hashing trick），零依赖兜底方案。

    哈希函数用 **blake2b（64 bit）而非 crc32**：短中文串下 crc32 的雪崩性较差，
    相近 n-gram 容易撞到同一位，256/512 维时碰撞噪声足以淹没真实匹配
    （表现为「无关文档比相关文档分更高」）；blake2b 把 2/3-gram 铺得更均匀，
    同维度下无关文本的余弦相似度稳定落在 0 附近。
    """

    def __init__(self, dim: int = 512) -> None:
        self.name = "hash"
        self.dim = max(16, int(dim))

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for gram in _grams(text):
            value = int.from_bytes(
                hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest(), "big"
            )
            # 低位选桶、最高位定符号：两者近似独立，抵消哈希冲突带来的系统性偏差
            vector[value % self.dim] += 1.0 if (value >> 63) & 1 else -1.0
        return _l2_normalize(vector)

    def _encode_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        async with _embedding_semaphore():
            if len(texts) >= _HASH_THREAD_THRESHOLD:
                return await asyncio.to_thread(self._encode_batch, list(texts))
            return self._encode_batch(texts)


class LocalEmbedder:
    """本地 sentence-transformers 模型（默认 `BAAI/bge-small-zh-v1.5`）。"""

    def __init__(self, model_name: str, dim: int = 512) -> None:
        self.name = f"local:{model_name}"
        self.dim = dim
        self._model_name = model_name
        self._model: Any = None
        self._load_lock = asyncio.Lock()

    async def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load_model)
        return self._model

    def _load_model(self) -> Any:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的明确提示
            raise EmbeddingUnavailableError(
                "未安装 sentence-transformers，无法使用本地嵌入模型；"
                "请执行 pip install -r requirements-ml.txt，或改用 EMBEDDING_BACKEND=hash"
            ) from exc
        logger.info("加载本地嵌入模型：%s", self._model_name)
        return SentenceTransformer(self._model_name)

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        async with _embedding_semaphore():
            model = await self._ensure_model()
            vectors = await asyncio.to_thread(
                model.encode,
                list(texts),
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            result = [[float(value) for value in vector] for vector in vectors]
            # 以模型实际输出维度为准（配置项可能与模型不一致）
            if result and len(result[0]) != self.dim:
                self.dim = len(result[0])
            return result


_embedder: Embedder | None = None
_embedder_key: tuple[str, str, int] | None = None


def build_embedder(settings: Settings) -> Embedder | None:
    """按配置构建嵌入后端；`none` 返回 None。"""
    backend = settings.embedding_backend_name
    if backend == "none":
        return None
    if backend == "local":
        return LocalEmbedder(settings.embedding_model, settings.embedding_dim)
    return HashEmbedder(settings.embedding_dim)


def get_embedder(settings: Settings | None = None) -> Embedder | None:
    """返回进程内缓存的嵌入后端；配置变更时自动重建。"""
    global _embedder, _embedder_key
    current = settings or get_settings()
    key = (
        current.embedding_backend_name,
        current.embedding_model,
        int(current.embedding_dim),
    )
    if _embedder_key != key:
        _embedder = build_embedder(current)
        _embedder_key = key
        logger.info("嵌入后端：%s", _embedder.name if _embedder else "none")
    return _embedder


def reset_embedder() -> None:
    """清空缓存（运行期配置变更或测试中调用）。"""
    global _embedder, _embedder_key
    _embedder = None
    _embedder_key = None


__all__ = [
    "Embedder",
    "HashEmbedder",
    "LocalEmbedder",
    "build_embedder",
    "cosine_similarity",
    "get_embedder",
    "reset_embedder",
]
