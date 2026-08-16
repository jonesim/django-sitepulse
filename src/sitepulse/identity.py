"""Visitor identity, without a cookie by default.

    visitor_hash = sha256(daily_salt + site_host + ip + user_agent)[:16]

The salt rotates every 24 hours and the old one is destroyed, so the same person
on the same device produces a different hash tomorrow. There is no mechanism,
even with full database access, to link a visitor across days -- which is the
whole point, and also the limitation: summing daily uniques over a month
overcounts, so never do it. Use :class:`~sitepulse.models.DailyUniqueVisitor`
for range-wide distinct counts instead.

Raw IP addresses and user-agent strings exist only in memory, for the few
milliseconds between the request finishing and the buffer flushing. Nothing here
writes either of them anywhere.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
from datetime import date as date_cls
from datetime import datetime, timedelta

from django.core.cache import caches
from django.utils import timezone

from .conf import sitepulse_settings

HASH_BYTES = 16
SALT_BYTES = 32

_salt_lock = threading.Lock()
_salt_cache: tuple[date_cls, bytes] | None = None


# ---------------------------------------------------------------------------
# salt
# ---------------------------------------------------------------------------


def current_salt(for_date: date_cls | None = None) -> bytes:
    """Today's salt, creating it if this is the first hit of the day.

    Stored in the database because every worker process has to agree on it: a
    per-process random salt would give one visitor a different hash in each
    Gunicorn worker, and a cache-only salt would vanish on a Redis restart and
    do the same thing mid-day.

    Creating a new day's salt destroys every older one, so rotation happens
    whether or not the nightly command runs.
    """
    global _salt_cache

    today = for_date or timezone.localdate()
    cached = _salt_cache
    if cached is not None and cached[0] == today:
        return cached[1]

    with _salt_lock:
        cached = _salt_cache
        if cached is not None and cached[0] == today:
            return cached[1]
        value = _load_or_create_salt(today)
        _salt_cache = (today, value)
        return value


def _load_or_create_salt(day: date_cls) -> bytes:
    from .models import Salt

    using = sitepulse_settings.DATABASE_ALIAS
    row, created = Salt.objects.using(using).get_or_create(
        date=day, defaults={"value": secrets.token_bytes(SALT_BYTES)}
    )
    if created:
        # Destroy every previous salt. Past that point the day's hashes cannot be
        # recomputed from an IP even by us, which is the property being claimed.
        Salt.objects.using(using).filter(date__lt=day).delete()
    return bytes(row.value)


def reset_salt_cache() -> None:
    """Forget the process-local salt (tests, and after a manual rotation)."""
    global _salt_cache
    _salt_cache = None


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def visitor_hash(host: str, ip: str, user_agent: str, for_date: date_cls | None = None) -> bytes:
    """The 16-byte daily-rotating visitor identifier."""
    digest = hashlib.sha256()
    digest.update(current_salt(for_date))
    digest.update(b"\x00")
    digest.update((host or "").encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update((ip or "").encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update((user_agent or "").encode("utf-8", "replace"))
    return digest.digest()[:HASH_BYTES]


def cookie_visitor_hash(cookie_value: str) -> bytes:
    """Stable identifier derived from the opt-in returning-visitor cookie.

    Hashed rather than stored raw so that a leaked database still doesn't hand
    anyone a value they can set as a cookie and impersonate.
    """
    return hashlib.sha256(b"sitepulse-vid" + cookie_value.encode("ascii", "replace")).digest()[
        :HASH_BYTES
    ]


def new_cookie_value() -> str:
    return secrets.token_hex(16)


def client_ip(request) -> str:
    """Best-effort client IP.

    Deliberately does **not** parse ``X-Forwarded-For``: behind a proxy that is
    trivially spoofable, and Django's own guidance is that the proxy should be
    the one to set ``REMOTE_ADDR``. If your deployment needs XFF, normalise it in
    a middleware ahead of this one.
    """
    return request.META.get("REMOTE_ADDR", "") or ""


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def _session_key(visitor: bytes) -> str:
    return "sitepulse:s:" + visitor.hex()


def assign_sessions(hits: list) -> None:
    """Stamp ``session_id`` and ``is_new_session`` onto a batch of pending hits.

    Runs in the flush thread, not the request path, and does one ``get_many`` /
    ``set_many`` per batch rather than two round-trips per hit. A cache miss just
    starts a new session, which is the correct fallback.
    """
    if not hits:
        return

    timeout = sitepulse_settings.SESSION_TIMEOUT_MINUTES * 60
    cache = caches[sitepulse_settings.CACHE_ALIAS]
    keys = {_session_key(hit.visitor_hash) for hit in hits}
    try:
        state: dict[str, tuple[str, float]] = dict(cache.get_many(keys))
    except Exception:  # pragma: no cover - a cache outage must not lose hits
        state = {}

    gap = timedelta(seconds=timeout)
    updates: dict[str, tuple[str, float]] = {}
    for hit in hits:
        key = _session_key(hit.visitor_hash)
        previous = updates.get(key) or state.get(key)
        ts: datetime = hit.ts
        if previous is not None and (ts.timestamp() - previous[1]) <= gap.total_seconds():
            hit.session_id = bytes.fromhex(previous[0])
            hit.is_new_session = False
        else:
            hit.session_id = os.urandom(HASH_BYTES)
            hit.is_new_session = True
        updates[key] = (hit.session_id.hex(), ts.timestamp())

    try:
        cache.set_many(updates, timeout=timeout)
    except Exception:  # pragma: no cover
        pass
