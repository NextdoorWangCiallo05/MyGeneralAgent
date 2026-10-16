"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { useAuth } from "@/hooks/useAuth";
import { ApiError, request, uploadFile } from "@/lib/api";
import type {
  ChunkListResponse,
  DocumentListResponse,
  KnowledgeChunk,
  KnowledgeDocument,
  KnowledgeSearchHit,
  KnowledgeSearchResponse,
  ReindexResponse,
  RuntimeConfig,
} from "@/lib/types";

/** 与后端 `knowledge_allowed_types` 默认值保持一致。 */
const ACCEPT = ".txt,.md,.markdown,.pdf";
const CHUNK_PAGE_SIZE = 20;

type Notice = { kind: "success" | "error" | "info"; text: string };

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function KnowledgePage() {
  const { user, logout } = useAuth();

  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  const [preview, setPreview] = useState<KnowledgeDocument | null>(null);
  const [chunks, setChunks] = useState<KnowledgeChunk[]>([]);
  const [chunkTotal, setChunkTotal] = useState(0);
  const [chunkOffset, setChunkOffset] = useState(0);
  const [chunkLoading, setChunkLoading] = useState(false);

  const [runtime, setRuntime] = useState<RuntimeConfig | null>(null);
  const [reindexing, setReindexing] = useState(false);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [hits, setHits] = useState<KnowledgeSearchHit[] | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);

  // ---------------------------------------------------------------- 数据加载
  const loadDocuments = useCallback(async () => {
    try {
      const data = await request<DocumentListResponse>("/api/v1/knowledge/documents");
      setDocuments(data.items);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return;
      setNotice({
        kind: "error",
        text: error instanceof ApiError ? error.message : "文档列表加载失败",
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadDocuments();
  }, [loadDocuments]);

  // 嵌入后端决定了「重建索引 / 检索测试」是否可用（未启用时按钮置灰并给出原因）
  useEffect(() => {
    void (async () => {
      try {
        setRuntime(await request<RuntimeConfig>("/api/v1/config"));
      } catch {
        setRuntime(null); // 读取失败时按「未启用」处理，保持 fail-closed
      }
    })();
  }, []);

  const knowledgeEnabled = runtime?.knowledge_enabled ?? false;

  const loadChunks = useCallback(async (documentId: string, offset: number) => {
    setChunkLoading(true);
    try {
      const data = await request<ChunkListResponse>(
        `/api/v1/knowledge/documents/${documentId}/chunks?limit=${CHUNK_PAGE_SIZE}&offset=${offset}`,
      );
      setChunks(data.items);
      setChunkTotal(data.total);
      setChunkOffset(data.offset);
    } catch (error) {
      setNotice({
        kind: "error",
        text: error instanceof ApiError ? error.message : "分块加载失败",
      });
    } finally {
      setChunkLoading(false);
    }
  }, []);

  // ---------------------------------------------------------------- 交互
  const handleUpload = useCallback(
    async (file: File) => {
      setUploading(true);
      setNotice(null);
      try {
        const created = await uploadFile<KnowledgeDocument>("/api/v1/knowledge/documents", file);
        setNotice({
          kind: "success",
          text: `《${created.filename}》已入库，共 ${created.chunk_count} 个分块（${created.char_count} 字）`,
        });
        await loadDocuments();
      } catch (error) {
        setNotice({
          kind: "error",
          text: error instanceof ApiError ? error.message : "上传失败，请稍后重试",
        });
      } finally {
        setUploading(false);
      }
    },
    [loadDocuments],
  );

  async function handlePick(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ""; // 允许重复选择同一文件
    if (file) await handleUpload(file);
  }

  async function handleDrop(event: React.DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file) await handleUpload(file);
  }

  const handlePreview = useCallback(
    async (document: KnowledgeDocument) => {
      setPreview(document);
      setChunks([]);
      setNotice(null);
      await loadChunks(document.id, 0);
    },
    [loadChunks],
  );

  const handleDelete = useCallback(
    async (document: KnowledgeDocument) => {
      if (!window.confirm(`确定删除《${document.filename}》及其全部分块吗？该操作不可恢复。`)) return;
      setNotice(null);
      try {
        await request(`/api/v1/knowledge/documents/${document.id}`, { method: "DELETE" });
        if (preview?.id === document.id) {
          setPreview(null);
          setChunks([]);
        }
        setNotice({ kind: "success", text: `《${document.filename}》已删除` });
        await loadDocuments();
      } catch (error) {
        setNotice({
          kind: "error",
          text: error instanceof ApiError ? error.message : "删除失败，请稍后重试",
        });
      }
    },
    [loadDocuments, preview],
  );

  const handleReindex = useCallback(
    async (force: boolean) => {
      const label = force
        ? "为全部分块重新计算向量（切换嵌入后端后必须执行）"
        : "为尚未向量化的分块补齐向量";
      if (force && !window.confirm(`${label}，确定继续吗？`)) return;
      setReindexing(true);
      setNotice(null);
      try {
        const result = await request<ReindexResponse>(
          `/api/v1/knowledge/reindex?force=${force}`,
          { method: "POST" },
        );
        setNotice({
          kind: "success",
          text:
            result.total === 0
              ? "所有分块都已向量化，无需重建。"
              : `重建完成：处理 ${result.total} 个分块，写入 ${result.embedded} 条向量（后端 ${result.backend}）。`,
        });
        await loadDocuments();
        if (preview) await loadChunks(preview.id, chunkOffset);
      } catch (error) {
        setNotice({
          kind: "error",
          text: error instanceof ApiError ? error.message : "重建索引失败，请稍后重试",
        });
      } finally {
        setReindexing(false);
      }
    },
    [chunkOffset, loadChunks, loadDocuments, preview],
  );

  const handleSearch = useCallback(async () => {
    const text = query.trim();
    if (!text) return;
    setSearching(true);
    setHits(null);
    setNotice(null);
    try {
      const data = await request<KnowledgeSearchResponse>(
        `/api/v1/knowledge/search?q=${encodeURIComponent(text)}`,
      );
      setHits(data.items);
    } catch (error) {
      setNotice({
        kind: "error",
        text: error instanceof ApiError ? error.message : "检索失败，请稍后重试",
      });
    } finally {
      setSearching(false);
    }
  }, [query]);

  const pageCount = Math.max(1, Math.ceil(chunkTotal / CHUNK_PAGE_SIZE));
  const pageIndex = Math.floor(chunkOffset / CHUNK_PAGE_SIZE);

  return (
    <div className="admin-page">
      <header className="admin-header">
        <span className="auth-logo" style={{ width: 28, height: 28, fontSize: 15 }} aria-hidden>
          ✦
        </span>
        <span className="admin-header-title">知识库</span>
        <span className="admin-tag">{user?.username ?? "用户"}</span>
        <div className="admin-header-actions">
          <Link href="/" className="btn btn-ghost" style={{ padding: "8px 14px" }}>
            返回对话
          </Link>
          <button
            type="button"
            className="btn btn-ghost"
            style={{ padding: "8px 14px" }}
            onClick={logout}
          >
            退出
          </button>
        </div>
      </header>

      <div className="admin-body">
        {notice ? <div className={`alert alert-${notice.kind}`}>{notice.text}</div> : null}

        <section className="admin-card">
          <h2>上传文档</h2>
          <p className="admin-card-desc">
            上传后自动完成解析、分块、向量化与入库，供知识库检索使用；单份文档不超过 5 MB。
          </p>

          <div
            className={`upload-zone ${dragging ? "is-dragging" : ""} ${uploading ? "is-busy" : ""}`}
            role="button"
            tabIndex={0}
            aria-label="选择或拖拽文档上传"
            onClick={() => {
              if (!uploading) inputRef.current?.click();
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !uploading) inputRef.current?.click();
            }}
            onDragOver={(event) => {
              event.preventDefault();
              if (!uploading) setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => void handleDrop(event)}
          >
            <input
              ref={inputRef}
              type="file"
              accept={ACCEPT}
              style={{ display: "none" }}
              onChange={(event) => void handlePick(event)}
            />
            <span className="upload-zone-icon" aria-hidden>
              ↑
            </span>
            <span className="upload-zone-title">
              {uploading ? "正在解析并入库…" : "点击选择文件，或拖拽到此处"}
            </span>
            <span className="upload-zone-hint">支持 txt / md / markdown / pdf</span>
          </div>
        </section>

        <section className="admin-card">
          <h2>索引与检索</h2>
          <p className="admin-card-desc">
            上传时已自动完成向量化。切换 <code>EMBEDDING_BACKEND</code> 后，
            旧向量与新查询不在同一向量空间，需执行「全量重算」。当前嵌入后端：
            <span
              className={`admin-badge ${knowledgeEnabled ? "is-active" : ""}`}
              style={{ marginLeft: 8 }}
            >
              {runtime?.knowledge_backend ?? "读取中…"}
            </span>
          </p>

          {runtime && !knowledgeEnabled ? (
            <div className="alert alert-info">
              当前未启用向量化（<code>EMBEDDING_BACKEND=none</code>），知识库检索不可用；
              在后端开启嵌入后端并重启后，回到本页重建索引即可。
            </div>
          ) : null}

          <div className="knowledge-actions">
            <button
              type="button"
              className="btn"
              disabled={reindexing || !knowledgeEnabled}
              onClick={() => void handleReindex(false)}
            >
              {reindexing ? "正在重建…" : "补齐索引"}
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              disabled={reindexing || !knowledgeEnabled}
              onClick={() => void handleReindex(true)}
            >
              全量重算
            </button>
          </div>

          <div className="knowledge-search">
            <input
              className="input"
              value={query}
              placeholder="输入问句测试召回，例如「部署默认监听哪个端口」"
              disabled={!knowledgeEnabled}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void handleSearch();
              }}
            />
            <button
              type="button"
              className="btn"
              disabled={searching || !knowledgeEnabled || !query.trim()}
              onClick={() => void handleSearch()}
            >
              {searching ? "检索中…" : "检索"}
            </button>
          </div>

          {hits ? (
            hits.length === 0 ? (
              <p className="knowledge-empty">
                没有命中任何片段：可能尚未上传相关文档，或索引尚未构建。
              </p>
            ) : (
              <div className="chunk-list">
                {hits.map((hit, index) => (
                  <article className="chunk-item" key={`${hit.document_id}-${hit.seq}-${index}`}>
                    <div className="chunk-head">
                      <span className="chunk-seq">
                        #{index + 1} {hit.filename} · 第 {hit.seq + 1} 块
                      </span>
                      <span className="chunk-meta">相似度 {hit.score.toFixed(4)}</span>
                    </div>
                    <pre className="chunk-body">{hit.content}</pre>
                  </article>
                ))}
              </div>
            )
          ) : null}
        </section>

        <section className="admin-card">
          <h2>我的文档</h2>
          <p className="admin-card-desc">共 {documents.length} 份文档。</p>

          {loading ? (
            <p className="knowledge-empty">正在加载…</p>
          ) : documents.length === 0 ? (
            <p className="knowledge-empty">还没有上传任何文档，先上传一份试试。</p>
          ) : (
            <table className="admin-table">
              <thead>
                <tr>
                  <th>文件名</th>
                  <th>类型</th>
                  <th>大小</th>
                  <th>状态</th>
                  <th>索引</th>
                  <th>分块</th>
                  <th>字符数</th>
                  <th>上传时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {documents.map((document) => (
                  <tr key={document.id}>
                    <td className="doc-name" title={document.filename}>
                      {document.filename}
                    </td>
                    <td>
                      <span className="admin-badge">{document.file_type.toUpperCase()}</span>
                    </td>
                    <td className="admin-mono">{formatSize(document.size_bytes)}</td>
                    <td>
                      <span
                        className={`admin-badge ${
                          document.status === "ready" ? "is-active" : ""
                        }`}
                      >
                        {document.status === "ready" ? "已入库" : "解析失败"}
                      </span>
                    </td>
                    <td>
                      <span
                        className={`admin-badge ${document.embedded ? "is-active" : ""}`}
                        title={
                          document.embedded
                            ? "分块已生成向量，可被知识库检索命中"
                            : "尚未生成向量，可点上方「补齐索引」"
                        }
                      >
                        {document.embedded ? "已向量化" : "未向量化"}
                      </span>
                    </td>
                    <td className="admin-mono">{document.chunk_count}</td>
                    <td className="admin-mono">{document.char_count}</td>
                    <td className="admin-mono">
                      {new Date(document.created_at).toLocaleString()}
                    </td>
                    <td>
                      <div className="doc-actions">
                        <button
                          type="button"
                          className="link-btn"
                          onClick={() => void handlePreview(document)}
                        >
                          查看分块
                        </button>
                        <button
                          type="button"
                          className="link-btn is-danger"
                          onClick={() => void handleDelete(document)}
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        {preview ? (
          <section className="admin-card knowledge-preview">
            <div className="knowledge-preview-head">
              <div>
                <h2>分块预览 · {preview.filename}</h2>
                <p className="admin-card-desc">
                  共 {chunkTotal} 个分块，每页 {CHUNK_PAGE_SIZE} 个（含重叠上下文）。
                </p>
              </div>
              <button
                type="button"
                className="btn btn-ghost"
                style={{ padding: "6px 12px" }}
                onClick={() => {
                  setPreview(null);
                  setChunks([]);
                }}
              >
                关闭
              </button>
            </div>

            {chunkLoading ? (
              <p className="knowledge-empty">正在加载分块…</p>
            ) : chunks.length === 0 ? (
              <p className="knowledge-empty">该文档暂无分块。</p>
            ) : (
              <div className="chunk-list">
                {chunks.map((chunk, index) => (
                  <article className="chunk-item" key={chunk.id}>
                    <div className="chunk-head">
                      <span className="chunk-seq">#{chunkOffset + index + 1}</span>
                      <span className="chunk-meta">
                        {chunk.char_count} 字 · {chunk.has_embedding ? "已向量化" : "未向量化"}
                      </span>
                    </div>
                    <pre className="chunk-body">{chunk.content}</pre>
                  </article>
                ))}
              </div>
            )}

            {pageCount > 1 ? (
              <div className="knowledge-pager">
                <button
                  type="button"
                  className="btn btn-ghost"
                  disabled={chunkLoading || pageIndex <= 0}
                  onClick={() => void loadChunks(preview.id, (pageIndex - 1) * CHUNK_PAGE_SIZE)}
                >
                  上一页
                </button>
                <span className="knowledge-pager-meta">
                  第 {pageIndex + 1} / {pageCount} 页
                </span>
                <button
                  type="button"
                  className="btn btn-ghost"
                  disabled={chunkLoading || pageIndex + 1 >= pageCount}
                  onClick={() => void loadChunks(preview.id, (pageIndex + 1) * CHUNK_PAGE_SIZE)}
                >
                  下一页
                </button>
              </div>
            ) : null}
          </section>
        ) : null}
      </div>
    </div>
  );
}
