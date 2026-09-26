"""Target Auto-Discovery pipeline API routes (mounted by the dashboard app).

POST /api/discovery/trigger-crawler    — on-demand network-graph crawl
POST /api/discovery/trigger-validator  — validate + promote the pending pool
GET  /api/discovery/stats              — discovered_targets lifecycle counts
"""

import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import APIRouter, HTTPException

from src.services.discussion_validator import start_background_validation, validator_status
from src.services.discovery_crawler import start_background_crawl

logger = logging.getLogger("discovery.pipeline.api")

router = APIRouter(prefix="/api/discovery", tags=["discovery-pipeline"])


@router.post("/trigger-crawler")
def trigger_crawler():
    """Kick off a background network-graph crawl over the active seeds."""
    result = start_background_crawl()
    if not result.get("ok"):
        raise HTTPException(409, result.get("detail", "crawler unavailable"))
    return result


@router.post("/trigger-validator")
def trigger_validator():
    """Validate + promote the PENDING_VALIDATION pool in the background."""
    result = start_background_validation()
    if not result.get("ok"):
        raise HTTPException(409, result.get("detail", "validator unavailable"))
    return result


@router.get("/stats")
def discovery_stats():
    """Pool lifecycle counts + live crawler/validator state for the dashboard."""
    stats = validator_status()
    from src.services.discovery_crawler import CRAWLER_STATE

    stats["crawler"] = CRAWLER_STATE.snapshot()
    return stats
