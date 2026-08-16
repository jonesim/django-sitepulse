"""Path, referrer and UTM normalisation.

Path cardinality is what makes homegrown analytics tables blow up, so this runs
on every hit and is deliberately boring.
"""

from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urlsplit

from .conf import sitepulse_settings

MAX_PATH = 255
MAX_REFERRER_HOST = 128
MAX_UTM = 64


@lru_cache(maxsize=1)
def _exclude_patterns() -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p) for p in sitepulse_settings.EXCLUDE_PATHS)


def reset_caches() -> None:
    _exclude_patterns.cache_clear()


def is_excluded(path: str) -> bool:
    """True if the path matches any ``EXCLUDE_PATHS`` regex."""
    return any(pattern.search(path) for pattern in _exclude_patterns())


def normalise_path(path: str) -> str:
    """Collapse the noise out of a request path.

    Query string is dropped (``utm_*`` is promoted to its own columns first),
    duplicate slashes collapse, a trailing slash is removed so ``/a`` and ``/a/``
    are one page, and the result is truncated to the column width.
    """
    if not path:
        return "/"
    path = path.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    if "//" in path:
        path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path[:MAX_PATH]


def normalise_route(route: str | None) -> str:
    """``resolver_match.route`` normalised to match :func:`normalise_path`.

    ``route`` is ``None`` for anything that never resolved -- 404s, or a
    middleware that short-circuited before URL resolution. Those get ``""`` and
    are found by ``status`` instead.
    """
    if not route:
        return ""
    if not route.startswith("/"):
        route = "/" + route
    if len(route) > 1 and route.endswith("/"):
        route = route[:-1]
    return route[:MAX_PATH]


def referrer_parts(referrer: str | None,
                   own_hosts: frozenset[str] = frozenset()) -> tuple[str, str]:
    """Split a Referer header into ``(host, path)``.

    Returns ``("", "")`` for direct traffic and for self-referrals -- an internal
    link is a navigation, not a traffic source, and counting it as one is the
    single most common way a homegrown sources report goes wrong.

    The path half is only kept when ``TRACK_REFERRER_PATH`` is on.
    """
    if not referrer:
        return "", ""
    try:
        parts = urlsplit(referrer)
    except ValueError:
        return "", ""
    host = (parts.hostname or "").lower()
    if not host or host in own_hosts:
        return "", ""
    host = host[:MAX_REFERRER_HOST]
    if not sitepulse_settings.TRACK_REFERRER_PATH:
        return host, ""
    return host, normalise_path(parts.path)


def utm_params(get_params) -> tuple[str, str, str]:
    """``(source, medium, campaign)`` from a ``QueryDict``-alike, truncated."""
    def one(name: str) -> str:
        value = get_params.get(name) or ""
        return value.strip()[:MAX_UTM]

    return one("utm_source"), one("utm_medium"), one("utm_campaign")


def own_hosts(request) -> frozenset[str]:
    """Hosts that count as "us" for self-referral detection."""
    hosts = set()
    try:
        host = request.get_host()
    except Exception:  # pragma: no cover - malformed Host header
        host = ""
    if host:
        hosts.add(host.split(":", 1)[0].lower())
    return frozenset(hosts)
