"""Partner API keys: dik_live_… / dik_test_…, shown once, stored only as SHA-256."""
from __future__ import annotations

import hashlib
import secrets
import string

_ALPHABET = string.ascii_letters + string.digits
_BODY_LEN = 32
PREFIXES = {"live": "dik_live_", "test": "dik_test_"}

SCOPES = frozenset({
    "nutrition:read",   # menu-item analysis
    "photos:analyze",   # photo analysis for the partner's users (gap 1)
    "usage:read",       # GET /v1/usage (billing/activity totals)
})

# Keys are for partner SERVERS only. Never ship one inside a mobile or web app.


def generate_key(mode: str) -> str:
    if mode not in PREFIXES:
        raise ValueError("mode must be 'live' or 'test'")
    return PREFIXES[mode] + "".join(secrets.choice(_ALPHABET) for _ in range(_BODY_LEN))


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_mode(key: str) -> str | None:
    """Return 'live' / 'test' for a well-formed key, else None."""
    for mode, prefix in PREFIXES.items():
        body = key[len(prefix):]
        if key.startswith(prefix) and len(body) == _BODY_LEN and all(c in _ALPHABET for c in body):
            return mode
    return None


def parse_bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"
