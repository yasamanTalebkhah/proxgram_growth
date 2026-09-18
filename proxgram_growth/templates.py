"""Comment template handling: validation, rendering, random selection.

Placeholders:
  {channel}       destination channel mention (e.g. @proxgram)
  {proxy_count}   number of fresh proxies currently listed
  {speed_note}    short speed descriptor picked per comment

Keep every template conversational, on-topic and non-aggressive: these are
posted as *comments* on public channels and must read like a helpful
community member, never like an ad blast.
"""

from __future__ import annotations

import random
import string
from typing import Mapping

# Conservative charset for rendered comments: predictable output, easy to
# unit-test, and avoids characters Telegram may parse as entities.
_ALLOWED_CHARS = set(string.ascii_letters + string.digits + " _-.,!?:;()'\"/@#%+*[]")
MAX_COMMENT_LENGTH = 1024


class TemplateError(ValueError):
    """Raised when a template cannot be rendered safely."""


DEFAULT_TEMPLATES: tuple[str, ...] = (
    "Fresh batch of fast proxies just landed in {channel} - {proxy_count} nodes, {speed_note}.",
    "Need quick market checks? Grab a low-latency node from {channel}. {proxy_count} fresh IPs online.",
    "PSA: new {speed_note} proxies are up in {channel} right now.",
    "Pool rotated - {proxy_count} clean IPs live in {channel}.",
    "Anyone testing tooling today: fresh {speed_note} nodes were added to {channel}.",
    "Updated the list in {channel}: {proxy_count} proxies, all {speed_note}.",
    "For folks monitoring tickers - low-latency options are available in {channel}.",
)

SPEED_NOTES: tuple[str, ...] = (
    "sub-second response times",
    "very low latency",
    "great uptime today",
    "fast connections",
)


class _SafeDict(dict):
    """format_map helper that raises on unknown placeholders."""

    def __missing__(self, key: str) -> str:
        raise KeyError(key)


def render_template(
    template: str,
    context: Mapping[str, object],
    *,
    rng: random.Random | None = None,
) -> str:
    """Render a template; TemplateError on unknown placeholders or bad output.

    Explicit failures matter here: a half-rendered comment posted publicly
    would look broken (or spammy) to readers and could invite reports.
    """
    try:
        rendered = template.format_map(_SafeDict(context))
    except (KeyError, IndexError) as exc:
        raise TemplateError(f"template has unsupported placeholder: {exc}") from None
    except ValueError as exc:
        raise TemplateError(f"invalid template format: {exc}") from None

    rendered = rendered.strip()
    if not rendered:
        raise TemplateError("rendered comment is empty")
    if len(rendered) > MAX_COMMENT_LENGTH:
        raise TemplateError("rendered comment exceeds Telegram length limit")
    for ch in rendered:
        if ch not in _ALLOWED_CHARS:
            raise TemplateError(f"unexpected character in comment: {ch!r}")
    return rendered


def render_random_with_template(
    templates: tuple[str, ...] | list[str],
    context: Mapping[str, object],
    *,
    rng: random.Random | None = None,
    avoid: str | None = None,
) -> tuple[str, str]:
    """Pick a random template and render it; returns (text, template).

    `avoid` is the last posted template for this channel: it is excluded so
    the same wording never repeats back-to-back (unless it is the only one).
    """
    if not templates:
        raise TemplateError("no templates available")
    rng = rng or random.Random()
    candidates = [t for t in templates if t != avoid] or list(templates)
    template = rng.choice(candidates)
    return render_template(template, context, rng=rng), template


def render_random(
    templates: tuple[str, ...] | list[str],
    context: Mapping[str, object],
    *,
    rng: random.Random | None = None,
    avoid: str | None = None,
) -> str:
    """Convenience wrapper returning only the rendered text."""
    text, _ = render_random_with_template(templates, context, rng=rng, avoid=avoid)
    return text
