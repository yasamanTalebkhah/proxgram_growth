"""Phase 2 discovery API routes (mounted by the dashboard app)."""

import logging
import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.services.discovery import (
    DEFAULT_KEYWORDS,
    KEYWORD_POOLS,
    discovery_status,
    start_background_discovery,
)

logger = logging.getLogger("discovery.api")

router = APIRouter(prefix="/api/channels/discover", tags=["discovery"])


class DiscoverRequest(BaseModel):
    """Optional custom keyword list + how many channels to accept this run."""

    keywords: Optional[List[str]] = Field(
        default=None,
        description="Custom Persian keywords (falls back to curated pools)",
        max_length=20,
    )
    limit: int = Field(
        default=5, ge=1, le=25,
        description="Maximum channels to accept and ingest this run",
    )
    max_keywords: Optional[int] = Field(
        default=None, ge=1, le=20,
        description="Cap on how many distinct keywords to search",
    )


@router.post("")
def start_discovery(payload: DiscoverRequest):
    """Kick off a background zero-join discovery run (never blocks)."""
    keywords = [k.strip() for k in (payload.keywords or []) if k.strip()] or None
    result = start_background_discovery(
        keywords=keywords, limit=payload.limit,
        max_keywords=payload.max_keywords,
    )
    if not result.get("ok"):
        raise HTTPException(409, result.get("detail", "discovery unavailable"))
    return result


@router.get("/status")
def get_discovery_status():
    """Engine state + recent discovery metrics (scanned/accepted/discarded)."""
    status = discovery_status()
    status["keyword_pools"] = KEYWORD_POOLS
    status["default_keywords"] = DEFAULT_KEYWORDS
    return status
