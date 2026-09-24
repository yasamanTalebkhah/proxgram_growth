"""Unit tests for the task seeder (pure logic — no DB required)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.seeder import parse_targets


def test_parse_targets_basic_commas():
    assert parse_targets("@a, @b,@c") == ["@a", "@b", "@c"]


def test_parse_targets_mixed_separators_and_ids():
    raw = "-1003481813519; @foo https://t.me/bar\n@baz"
    assert parse_targets(raw) == ["-1003481813519", "@foo", "https://t.me/bar", "@baz"]


def test_parse_targets_empty_and_whitespace_only():
    assert parse_targets("") == []
    assert parse_targets("   ,\t;; \n ") == []
