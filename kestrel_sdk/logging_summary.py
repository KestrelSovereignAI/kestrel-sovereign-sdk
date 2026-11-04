"""Non-content summaries of caller text for log lines.

Mirrors the host's ``logging_config.content_log_summary`` so SDK log records
carry the same facts a host's do.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# Keys the digest. Random per process and never written anywhere: an unkeyed
# hash of a short message ("yes", a name) is an offline oracle for guessing
# what it said, while a keyed one still lets two log lines of one run be
# matched to the same text.
_CONTENT_DIGEST_KEY = secrets.token_bytes(32)
CONTENT_DIGEST_HEX_CHARS = 12


def content_log_summary(text: str) -> str:
    """Describe caller content for a log line without any of its text.

    Log files are plaintext with no retention bound, so a record gets
    non-content facts only: ``chars=N digest=<12 hex>``, the length and a short
    process-keyed HMAC, never the text or a prefix of it.
    """
    # ``surrogatepass``: JSON admits lone surrogates, and a log line must not
    # be the thing that fails the call.
    encoded = text.encode("utf-8", errors="surrogatepass")
    digest = hmac.new(_CONTENT_DIGEST_KEY, encoded, hashlib.sha256)
    return f"chars={len(text)} digest={digest.hexdigest()[:CONTENT_DIGEST_HEX_CHARS]}"
