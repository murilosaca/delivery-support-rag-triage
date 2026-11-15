"""Policy chunking, Gemini embeddings and ChromaDB retrieval."""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

os.environ.setdefault("ANONYMIZED_TELEMETRY", "FALSE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import chromadb
from google import genai
from google.genai import types

from app.config import Settings

logger = logging.getLogger(__name__)

COLLECTION_NAME = "politicas_suporte"
AUTHORITY_CHUNK_ID = "regra-5"
_POLICY_FILENAME = "politicas_suporte.md"
_SECTION_PATTERN = re.compile(r"(?m)^##\s+(.+?)\s*$")
_MODEL_UNAVAILABLE_MARKERS = (
    "not found",
    "not_found",
    "404",
    "deprecated",
    "was shut down",
    "shutdown",
    "no longer available",
    "unknown model",
    "invalid model",
    "is not supported",
    "unsupported model",
)
_AUTH_ERROR_MARKERS = (
    "api key",
    "api_key",
    "permission denied",
    "unauthenticated",
    "unauthorized",
    " 401",
    " 403",
)


class RAGServiceError(Exception):
    """Raised when indexing or retrieval cannot complete."""


@dataclass(frozen=True)
class PolicyChunk:
    """One policy section ready to embed."""

    chunk_id: str
    title: str
    text: str
    index: int


@dataclass(frozen=True)
class RetrievedChunk:
    """A policy section returned by the vector search."""

    chunk_id: str
    title: str
    text: str
    metadata: dict[str, Any]
    distance: float | None


def parse_policy_markdown(markdown: str) -> list[PolicyChunk]:
    """Split a markdown document into one chunk per ``##`` section."""

    matches = list(_SECTION_PATTERN.finditer(markdown))
    if not matches:
        raise RAGServiceError("Policy document has no '##' sections to index")

    chunks: list[PolicyChunk] = []
    for position, match in enumerate(matches):
        start = match.end()
        next_position = position + 1
        end = matches[next_position].start() if next_position < len(matches) else len(markdown)
        title = match.group(1).strip()
        body = markdown[start:end].strip()
        text = f"## {title}\n\n{body}" if body else f"## {title}"
        chunks.append(
            PolicyChunk(
                chunk_id=f"regra-{position + 1}",
                title=title,
                text=text,
                index=position + 1,
            )
        )
    return chunks


def _truncate_error(exc: Exception, limit: int = 400) -> str:
    text = str(exc).strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _is_model_unavailable(exc: Exception) -> bool:
    text = f" {exc}".lower()
    if any(marker in text for marker in _AUTH_ERROR_MARKERS):
        return False
    return any(marker in text for marker in _MODEL_UNAVAILABLE_MARKERS)


def _is_task_type_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "task_type" in text or "tasktype" in text


class RAGService:
    """Index ``data/politicas_suporte.md`` and retrieve the top sections."""

    def __init__(self, settings: Settings, policy_path: Path | None = None) -> None:
        self._settings = settings
        self._policy_path = policy_path or (
            Path(__file__).resolve().parent.parent / "data" / _POLICY_FILENAME
        )
        self._persist_dir = Path(settings.chroma_persist_dir)
        self._lock = threading.Lock()
        self._ingest_lock = threading.Lock()
        self._chroma: chromadb.PersistentClient | None = None
        self._collection: Any = None
        self._genai: genai.Client | None = None
        self._active_embedding_model = settings.embedding_model
        self._model_pinned = False
        stored_model = self._read_model_marker()
        if stored_model:
            self._active_embedding_model = stored_model
            self._model_pinned = True

    @property
    def collection_name(self) -> str:
        return COLLECTION_NAME

    def _marker_path(self) -> Path:
        return self._persist_dir / ".embedding_model"

    def _read_model_marker(self) -> str | None:
        path = self._marker_path()
        try:
            if not path.is_file():
                return None
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("Could not read the embedding model marker: %s", exc)
            return None
        return value or None

    def _write_model_marker(self, model: str) -> None:
        path = self._marker_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(model, encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not persist the embedding model marker: %s", exc)

    def _genai_client(self) -> genai.Client:
        if not self._settings.gemini_api_key:
            raise RAGServiceError("GEMINI_API_KEY is not configured")
        if self._genai is None:
            self._genai = genai.Client(
                api_key=self._settings.gemini_api_key,
                http_options=types.HttpOptions(timeout=self._settings.gemini_timeout_ms),
            )
        return self._genai

    def _chroma_client(self) -> chromadb.PersistentClient:
        if self._chroma is None:
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            client_path = str(self._persist_dir)
            settings_cls = getattr(chromadb.config, "Settings", None)
            if settings_cls is None:
                self._chroma = chromadb.PersistentClient(path=client_path)
            else:
                try:
                    self._chroma = chromadb.PersistentClient(
                        path=client_path,
                        settings=settings_cls(anonymized_telemetry=False),
                    )
                except Exception as exc:
                    logger.warning("Chroma settings were rejected (%s); opening the default client", exc)
                    self._chroma = chromadb.PersistentClient(path=client_path)
        return self._chroma

    def _get_collection(self) -> Any:
        if self._collection is None:
            self._collection = self._chroma_client().get_or_create_collection(
                name=COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    def count(self) -> int:
        """Return the number of indexed policy sections."""

        with self._lock:
            try:
                return int(self._get_collection().count())
            except Exception as exc:
                raise RAGServiceError(f"Could not read the vector collection: {_truncate_error(exc)}") from exc

    def load_chunks(self) -> list[PolicyChunk]:
        """Parse the policy file from disk."""

        if not self._policy_path.is_file():
            raise RAGServiceError(f"Policy file not found: {self._policy_path}")
        try:
            markdown = self._policy_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RAGServiceError(f"Could not read the policy file: {exc}") from exc
        return parse_policy_markdown(markdown)

    def ingest(self) -> int:
        """Re-embed every policy section and replace the collection."""

        with self._ingest_lock:
            chunks = self.load_chunks()
            vectors = self.embed_documents([chunk.text for chunk in chunks])
            if len(vectors) != len(chunks):
                raise RAGServiceError("Embedding count does not match the number of policy sections")
            self._replace_collection(chunks, vectors)
            self._write_model_marker(self._active_embedding_model)
            logger.info(
                "Indexed %s policy sections with embedding model %s",
                len(chunks),
                self._active_embedding_model,
            )
            return len(chunks)

    def _replace_collection(self, chunks: list[PolicyChunk], vectors: list[list[float]]) -> None:
        metadatas = [
            {
                "section_title": chunk.title,
                "section_index": chunk.index,
                "chunk_id": chunk.chunk_id,
                "source": f"data/{_POLICY_FILENAME}",
            }
            for chunk in chunks
        ]
        with self._lock:
            client = self._chroma_client()
            try:
                client.delete_collection(COLLECTION_NAME)
            except Exception as exc:
                logger.info("No previous collection to replace (%s)", type(exc).__name__)
            self._collection = None
            collection = self._get_collection()
            existing = collection.get()
            existing_ids = list(existing.get("ids") or [])
            if existing_ids:
                collection.delete(ids=existing_ids)
            collection.upsert(
                ids=[chunk.chunk_id for chunk in chunks],
                embeddings=vectors,
                documents=[chunk.text for chunk in chunks],
                metadatas=metadatas,
            )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed policy sections for retrieval."""

        return self._embed(texts, task_type="RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        """Embed one customer message for retrieval."""

        vectors = self._embed([text], task_type="RETRIEVAL_QUERY")
        return vectors[0]

    def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        if not texts:
            raise RAGServiceError("Cannot embed an empty text batch")
        models = self._models_to_try()
        last_error: Exception | None = None
        for model in models:
            try:
                vectors = self._embed_with_model(model, texts, task_type)
            except RAGServiceError:
                raise
            except Exception as exc:
                last_error = exc
                if not _is_model_unavailable(exc):
                    raise RAGServiceError(f"Embedding request failed: {_truncate_error(exc)}") from exc
                logger.warning("Embedding model %s is unavailable: %s", model, _truncate_error(exc))
                continue
            if model != self._settings.embedding_model:
                logger.warning(
                    "Using fallback embedding model %s because %s is unavailable",
                    model,
                    self._settings.embedding_model,
                )
            self._active_embedding_model = model
            self._model_pinned = True
            return vectors
        raise RAGServiceError(
            f"No embedding model is available. Last error: {_truncate_error(last_error) if last_error else 'unknown'}"
        )

    def _models_to_try(self) -> list[str]:
        if self._model_pinned:
            return [self._active_embedding_model]
        models = [self._settings.embedding_model]
        fallback = self._settings.embedding_model_fallback
        if fallback and fallback not in models:
            models.append(fallback)
        return models

    def _embed_with_model(self, model: str, texts: list[str], task_type: str) -> list[list[float]]:
        client = self._genai_client()
        try:
            response = client.models.embed_content(
                model=model,
                contents=texts,
                config=types.EmbedContentConfig(task_type=task_type),
            )
        except Exception as exc:
            if not _is_task_type_error(exc):
                raise
            logger.info("Embedding model %s rejected task_type; retrying without it", model)
            response = client.models.embed_content(model=model, contents=texts)
        return _vectors_from_response(response, expected=len(texts))

    def retrieve(self, query: str, top_k: int | None = None) -> list[RetrievedChunk]:
        """Return the closest policy sections for a customer message."""

        limit = top_k if top_k is not None else self._settings.rag_top_k
        total = self.count()
        if total <= 0:
            return []
        vector = self.embed_query(query)
        n_results = max(1, min(limit, total))
        with self._lock:
            result = self._get_collection().query(
                query_embeddings=[vector],
                n_results=n_results,
                include=["documents", "metadatas", "distances"],
            )
        return _chunks_from_query(result)

    def get_chunk(self, chunk_id: str) -> RetrievedChunk | None:
        """Load one section by its stable id, without a new embedding call."""

        with self._lock:
            try:
                found = self._get_collection().get(
                    ids=[chunk_id],
                    include=["documents", "metadatas"],
                )
            except Exception as exc:
                raise RAGServiceError(f"Could not load policy chunk {chunk_id}: {_truncate_error(exc)}") from exc
        ids = list(found.get("ids") or [])
        if not ids:
            return None
        documents = list(found.get("documents") or [])
        metadatas = list(found.get("metadatas") or [])
        metadata = dict(metadatas[0] or {}) if metadatas else {}
        title = str(metadata.get("section_title") or chunk_id)
        text = documents[0] if documents and documents[0] else ""
        return RetrievedChunk(
            chunk_id=str(ids[0]),
            title=title,
            text=str(text),
            metadata=metadata,
            distance=None,
        )

    def retrieve_for_request(self, user_message: str, order_value: float) -> list[RetrievedChunk]:
        """Retrieve the top sections and always include the authority rule above the cap."""

        chunks = self.retrieve(user_message)
        if order_value > self._settings.max_automatic_refund_brl:
            authority = self.get_chunk(AUTHORITY_CHUNK_ID)
            if authority is not None and all(chunk.chunk_id != authority.chunk_id for chunk in chunks):
                chunks.append(authority)
        logger.info(
            "Retrieved %s policy section(s): %s",
            len(chunks),
            ", ".join(chunk.title for chunk in chunks) or "(none)",
        )
        return chunks


def _vectors_from_response(response: object, expected: int) -> list[list[float]]:
    embeddings = list(getattr(response, "embeddings", None) or [])
    if len(embeddings) != expected:
        raise RAGServiceError(f"Expected {expected} embeddings and received {len(embeddings)}")
    vectors: list[list[float]] = []
    for index, item in enumerate(embeddings):
        values = list(getattr(item, "values", None) or [])
        if not values:
            raise RAGServiceError(f"Gemini returned an empty embedding at position {index}")
        vectors.append([float(value) for value in values])
    return vectors


def _chunks_from_query(result: dict[str, Any]) -> list[RetrievedChunk]:
    ids = list((result.get("ids") or [[]])[0] or [])
    documents = list((result.get("documents") or [[]])[0] or [])
    metadatas = list((result.get("metadatas") or [[]])[0] or [])
    distances = list((result.get("distances") or [[]])[0] or [])
    chunks: list[RetrievedChunk] = []
    for index, chunk_id in enumerate(ids):
        metadata = dict(metadatas[index] or {}) if index < len(metadatas) and metadatas[index] else {}
        document = documents[index] if index < len(documents) and documents[index] else ""
        distance: float | None = None
        if index < len(distances) and distances[index] is not None:
            distance = float(distances[index])
        chunks.append(
            RetrievedChunk(
                chunk_id=str(chunk_id),
                title=str(metadata.get("section_title") or chunk_id),
                text=str(document),
                metadata=metadata,
                distance=distance,
            )
        )
    return chunks
