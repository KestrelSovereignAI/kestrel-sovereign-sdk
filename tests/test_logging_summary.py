"""``content_log_summary`` describes caller text without any of it (#105)."""

from __future__ import annotations

import hashlib
import re

from kestrel_sdk.logging_summary import content_log_summary

_SHAPE = re.compile(r"chars=(\d+) digest=([0-9a-f]{12})")


def test_summary_is_length_and_a_short_digest():
    match = _SHAPE.fullmatch(content_log_summary("private text"))
    assert match is not None
    assert int(match.group(1)) == len("private text")


def test_summary_contains_none_of_the_text():
    assert "MARKER" not in content_log_summary("MARKER-caller-content")


def test_same_text_matches_within_the_process_and_different_text_does_not():
    assert content_log_summary("yes") == content_log_summary("yes")
    assert content_log_summary("yes") != content_log_summary("no")


def test_digest_is_keyed_not_a_plain_hash():
    """An unkeyed hash of a short message is an offline guessing oracle."""
    digest = _SHAPE.fullmatch(content_log_summary("yes")).group(2)
    assert digest != hashlib.sha256(b"yes").hexdigest()[:12]


def test_lone_surrogate_and_empty_text_do_not_raise():
    assert _SHAPE.fullmatch(content_log_summary("\ud800")) is not None
    assert content_log_summary("").startswith("chars=0 digest=")
