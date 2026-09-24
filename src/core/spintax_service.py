"""Spintax preview service for the dashboard studio (pure function wrapper)."""

import os
from typing import List

from src.core.templates import SpintaxEngine

PREVIEW_LIMIT = 5


def spin_preview(template: str, count: int = 3) -> List[str]:
    """Render N unique variations of a template through the real engine."""
    count = max(1, min(count, PREVIEW_LIMIT))
    destination = os.getenv("GROWTH_DESTINATION_CHANNEL", "").strip() or "@proxgram"
    variants: List[str] = []
    for _ in range(count):
        rendered = SpintaxEngine.render_promo(template, destination)
        if rendered not in variants:
            variants.append(rendered)
    return variants
