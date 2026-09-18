"""Unit tests for template rendering and random selection."""

from __future__ import annotations

import random

import pytest

from templates import (
    DEFAULT_TEMPLATES,
    TemplateError,
    render_random_with_template,
    render_template,
)

CONTEXT = {"channel": "@proxgram", "proxy_count": 42, "speed_note": "very low latency"}


def test_all_default_templates_render_with_standard_context():
    for template in DEFAULT_TEMPLATES:
        rendered = render_template(template, CONTEXT)
        assert "@proxgram" in rendered
        assert "{" not in rendered and "}" not in rendered


def test_render_substitutes_all_placeholders():
    text = render_template(
        "PSA: new {speed_note} proxies are up in {channel} right now.", CONTEXT
    )
    assert text == "PSA: new very low latency proxies are up in @proxgram right now."


def test_unknown_placeholder_raises_instead_of_half_rendering():
    with pytest.raises(TemplateError):
        render_template("Broken {nonexistent} template", CONTEXT)


def test_empty_template_is_rejected():
    with pytest.raises(TemplateError):
        render_template("   ", CONTEXT)


def test_oversized_comment_is_rejected():
    long_channel = "@" + "x" * 1100
    with pytest.raises(TemplateError):
        render_template("Visit {channel} now", {"channel": long_channel, "proxy_count": 1, "speed_note": "fast"})


def test_avoid_prevents_immediate_repeat():
    templates = tuple(DEFAULT_TEMPLATES)
    rng = random.Random(99)
    seen_repeats = 0
    previous = None
    for _ in range(200):
        text, template = render_random_with_template(templates, CONTEXT, rng=rng, avoid=previous)
        assert template != previous or len(templates) == 1
        if template == previous:
            seen_repeats += 1
        previous = template
    assert seen_repeats == 0


def test_avoid_falls_back_when_single_template():
    text, template = render_random_with_template(
        ("Only template {channel}",), CONTEXT, rng=random.Random(1), avoid="Only template {channel}"
    )
    assert text == "Only template @proxgram"


def test_render_random_returns_text_only():
    from templates import render_random

    text = render_random(DEFAULT_TEMPLATES, CONTEXT, rng=random.Random(3))
    assert isinstance(text, str) and "@proxgram" in text


def test_no_templates_raises():
    with pytest.raises(TemplateError):
        render_random_with_template([], CONTEXT)
