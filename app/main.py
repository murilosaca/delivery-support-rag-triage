"""FastAPI application for delivery-support triage."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import ValidationError

os.environ.setdefault("ANONYMIZED_TELEMETRY", "FALSE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from app import __version__
from app.cache import TriageCache, build_cache_key, cache_bypass_requested
from app.config import Settings, get_settings
from app.llm_service import LLMService, LLMServiceError, injection_decision, is_prompt_injection
from app.rag_service import RAGService, RAGServiceError
from app.schemas import HealthResponse, IngestResponse, LLMTriageDecision, TriageRequest, TriageResponse

logger = logging.getLogger(__name__)


@dataclass
class AppServices:
    """Dependencies created for the process lifetime."""

    settings: Settings
    rag: RAGService
    cache: TriageCache
    llm: LLMService


def configure_logging() -> None:
    """Configure process logging once."""

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open dependencies and index the policy file when the collection is empty."""

    configure_logging()
    settings = get_settings()
    rag = RAGService(settings)
    cache = TriageCache(settings.redis_url, settings.cache_ttl_seconds)
    llm = LLMService(settings)
    app.state.services = AppServices(settings=settings, rag=rag, cache=cache, llm=llm)
    try:
        if not settings.gemini_api_key:
            logger.error("GEMINI_API_KEY is empty; ingest and triage stay unavailable")
        try:
            count = await asyncio.to_thread(rag.count)
        except RAGServiceError:
            logger.exception("Could not read ChromaDB during startup")
            count = 0
        if count == 0 and settings.gemini_api_key:
            try:
                indexed = await asyncio.to_thread(rag.ingest)
                logger.info("Startup ingest indexed %s policy sections", indexed)
            except Exception:
                logger.exception("Startup ingest failed; POST /ingest retries it")
        elif count:
            logger.info("Policy collection already contains %s sections", count)
        yield
    finally:
        await cache.close()


app = FastAPI(
    title="Delivery Support RAG Triage",
    summary="Triagem de suporte de delivery com RAG, strict grounding e structured outputs.",
    version=__version__,
    lifespan=lifespan,
)


def _services(request: Request) -> AppServices:
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(status_code=503, detail="Service is not ready")
    return services


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _build_response(
    ticket_id: str,
    decision: LLMTriageDecision,
    *,
    cached: bool,
    started: float,
) -> TriageResponse:
    response = TriageResponse(
        ticket_id=ticket_id,
        cached=cached,
        latency_ms=_elapsed_ms(started),
        **decision.model_dump(),
    )
    logger.info(
        "triage ticket_id=%s category=%s action=%s grounded=%s cached=%s latency_ms=%s",
        response.ticket_id,
        response.category,
        response.recommended_action,
        response.is_grounded,
        response.cached,
        response.latency_ms,
    )
    return response


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    """Point browsers at the operational endpoints."""

    return {
        "service": "delivery-support-rag-triage",
        "health": "/health",
        "docs": "/docs",
    }


@app.get("/health", response_model=HealthResponse, tags=["operations"])
async def health(request: Request) -> HealthResponse:
    """Report API liveness, Redis reachability and the indexed document count."""

    services = _services(request)
    redis_ok = await services.cache.ping()
    try:
        documents = await asyncio.to_thread(services.rag.count)
    except RAGServiceError:
        logger.exception("ChromaDB health check failed")
        documents = 0
    status = "ok" if redis_ok and documents > 0 else "degraded"
    return HealthResponse(
        status=status,
        api="ok",
        redis="ok" if redis_ok else "unavailable",
        chroma_documents=documents,
    )


@app.post("/ingest", response_model=IngestResponse, tags=["ingestion"])
async def ingest(request: Request) -> IngestResponse:
    """Re-chunk the policy file, embed each section and replace the vector index."""

    services = _services(request)
    if not services.settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY is not configured")
    try:
        indexed = await asyncio.to_thread(services.rag.ingest)
    except RAGServiceError as exc:
        logger.warning("Ingest failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return IngestResponse(
        documents_indexed=indexed,
        collection=services.rag.collection_name,
        persist_dir=services.settings.chroma_persist_dir,
    )


@app.post("/triage", response_model=TriageResponse, tags=["triage"])
async def triage(
    payload: TriageRequest,
    request: Request,
    x_bypass_cache: Annotated[str | None, Header()] = None,
) -> TriageResponse:
    """Cache lookup, then RAG retrieval and a schema-constrained Gemini decision.

    Send ``X-Bypass-Cache: true`` to ignore a stored decision and recompute it.
    The fresh decision is still written back to the cache.
    """

    started = time.perf_counter()
    services = _services(request)
    cache_key = build_cache_key(payload.user_message, payload.order_value)
    bypass = cache_bypass_requested(x_bypass_cache)

    try:
        if not bypass:
            cached = await services.cache.get(cache_key)
            if cached is not None:
                try:
                    decision = LLMTriageDecision.model_validate(cached)
                except ValidationError:
                    logger.warning("Ignoring a cached decision that no longer matches the schema")
                else:
                    return _build_response(payload.ticket_id, decision, cached=True, started=started)

        if is_prompt_injection(payload.user_message):
            decision = injection_decision()
        else:
            if not services.settings.gemini_api_key:
                raise HTTPException(status_code=503, detail="GEMINI_API_KEY is not configured")
            policies = await asyncio.to_thread(
                services.rag.retrieve_for_request,
                payload.user_message,
                payload.order_value,
            )
            decision = await asyncio.to_thread(
                services.llm.decide,
                payload.user_message,
                payload.order_value,
                policies,
            )
        await services.cache.set(cache_key, decision.model_dump(mode="json"))
        return _build_response(payload.ticket_id, decision, cached=False, started=started)
    except HTTPException:
        raise
    except (LLMServiceError, RAGServiceError) as exc:
        logger.warning("Triage failed for ticket_id=%s: %s", payload.ticket_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
