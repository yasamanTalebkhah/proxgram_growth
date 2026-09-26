"""Target Auto-Discovery pipeline + Target Studio API routes.

Pipeline (commit 41d2c92):
  POST /api/discovery/trigger-crawler    — on-demand network-graph crawl
  POST /api/discovery/trigger-validator  — validate + promote the pending pool
  GET  /api/discovery/stats              — lifecycle counts + engine state

Target Studio (this panel):
  GET    /api/discovery/targets              — filtered, paginated pool listing
  POST   /api/discovery/targets/{id}/reprobe        — reset to PENDING_VALIDATION + re-probe
  POST   /api/discovery/targets/{id}/force-promote  — manual promote (bypasses checks)
  DELETE /api/discovery/targets/{id}         — delete a pool record
  POST   /api/discovery/purge-disqualified   — clean up disqualified records
"""

import logging
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.services.discovery_studio import (
    delete_target_record,
    force_promote_target,
    list_targets,
    purge_disqualified,
    reprobe_target,
    studio_stats,
)
from src.services.discussion_validator import start_background_validation, validator_status
from src.services.discovery_crawler import start_background_crawl

logger = logging.getLogger("discovery.studio.api")

router = APIRouter(prefix="/api/discovery", tags=["discovery-studio"])

VALID_STATUSES = {"PENDING_VALIDATION", "VALIDATED_HAS_DISCUSSION",
                  "DISQUALIFIED_NO_DISCUSSION", "FAILED"}


class CrawlRequest(BaseModel):
    limit_seeds: int = Field(default=5, ge=1, le=25,
                             description="Max seed channels to sweep this run")


class ValidateRequest(BaseModel):
    batch_size: int = Field(default=25, ge=1, le=100,
                            description="Max PENDING_VALIDATION targets this run")


class PurgeRequest(BaseModel):
    days: Optional[int] = Field(default=None, ge=1, le=365,
                                description="Only purge disqualified records older than this many days; omit for all")


@router.post("/trigger-crawler")
def trigger_crawler(payload: CrawlRequest | None = None):
    """Kick off a background network-graph crawl over up to `limit_seeds` seeds."""
    limit_seeds = payload.limit_seeds if payload else 5
    result = start_background_crawl(max_seeds=limit_seeds)
    if not result.get("ok"):
        raise HTTPException(409, result.get("detail", "crawler unavailable"))
    from src.services.discovery_crawler import CRAWLER_STATE

    result["limit_seeds"] = limit_seeds
    result["seeds_planned"] = list(CRAWLER_STATE.seeds)
    return result


@router.post("/trigger-validator")
def trigger_validator(payload: ValidateRequest | None = None):
    """Validate + promote up to `batch_size` PENDING_VALIDATION targets in the background."""
    batch_size = payload.batch_size if payload else 25
    result = start_background_validation(limit=batch_size)
    if not result.get("ok"):
        raise HTTPException(409, result.get("detail", "validator unavailable"))
    result["batch_size"] = batch_size
    return result


@router.get("/stats")
def discovery_stats():
    """Flat studio metrics + pool breakdown + live crawler/validator state."""
    stats = validator_status()
    from src.services.discovery_crawler import CRAWLER_STATE

    stats["crawler"] = CRAWLER_STATE.snapshot()
    stats.update(studio_stats())
    return stats


@router.get("/targets")
def discovery_targets(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: Optional[str] = Query(default=None),
):
    """Filtered + paginated discovered_targets listing with disqualification reasons."""
    if status and status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(VALID_STATUSES)}")
    return list_targets(status=status, search=search, limit=limit, offset=offset)


@router.post("/targets/{target_id}/reprobe")
def discovery_reprobe(target_id: int):
    """Reset a target to PENDING_VALIDATION and trigger a single-item probe."""
    if not reprobe_target(target_id):
        raise HTTPException(404, "discovered target not found")
    result = start_background_validation(single_id=target_id)
    if not result.get("ok"):
        raise HTTPException(409, result.get("detail", "validator unavailable"))
    return {"ok": True, "detail": result["detail"], "target_id": target_id}


@router.post("/targets/{target_id}/force-promote")
def discovery_force_promote(target_id: int):
    """Manually promote a pool target to target_channels (tag='manual_promoted')."""
    result = force_promote_target(target_id)
    if result is None:
        raise HTTPException(404, "discovered target not found")
    return {"ok": True,
            "inserted": result,
            "detail": "promoted" if result else "already in target_channels"}


@router.delete("/targets/{target_id}")
def discovery_delete_target(target_id: int):
    """Delete a record from discovered_targets."""
    if not delete_target_record(target_id):
        raise HTTPException(404, "discovered target not found")
    return {"ok": True, "deleted": target_id}


@router.post("/purge-disqualified")
def discovery_purge_disqualified(payload: PurgeRequest | None = None):
    """Delete disqualified records (all, or only older than `days`)."""
    days = payload.days if payload else None
    return {"ok": True, "deleted": purge_disqualified(days=days)}
