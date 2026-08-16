"""Turning a user-agent string and an IP into the handful of coarse values we keep.

All of this runs in the flush thread, off the request path, on data that only
ever lived in memory.
"""

from __future__ import annotations

import re
import threading
from functools import lru_cache

from .conf import sitepulse_settings
from .models import Browser, Device, OperatingSystem

try:  # pragma: no cover - exercised by the absence test only
    from user_agents import parse as _parse_ua
except ImportError:  # pragma: no cover
    _parse_ua = None

MAX_NAME = 64

_lookup_lock = threading.Lock()
_lookup_cache: dict[tuple[str, str], int] = {}


# ---------------------------------------------------------------------------
# bots
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _bot_re() -> re.Pattern:
    patterns = list(sitepulse_settings.BOT_UA_PATTERNS) + list(
        sitepulse_settings.EXTRA_BOT_UA_PATTERNS
    )
    return re.compile("|".join(f"(?:{p})" for p in patterns) or r"(?!)", re.IGNORECASE)


def is_bot(user_agent: str) -> bool:
    """Flag, never drop -- a wrong boolean is a backfill, a discarded row is gone."""
    if not user_agent:
        # No UA at all is overwhelmingly automated traffic.
        return True
    return bool(_bot_re().search(user_agent))


# ---------------------------------------------------------------------------
# user agents
# ---------------------------------------------------------------------------

_MOBILE_RE = re.compile(r"mobi|iphone|ipod|android.*mobile|windows phone", re.IGNORECASE)
_TABLET_RE = re.compile(r"ipad|tablet|kindle|silk|playbook", re.IGNORECASE)
_BROWSER_RES = (
    ("Edge", re.compile(r"edg[ea]?/", re.IGNORECASE)),
    ("Opera", re.compile(r"opr/|opera", re.IGNORECASE)),
    ("Samsung Internet", re.compile(r"samsungbrowser", re.IGNORECASE)),
    ("Firefox", re.compile(r"firefox/|fxios", re.IGNORECASE)),
    ("Chrome", re.compile(r"chrome/|crios", re.IGNORECASE)),
    ("Safari", re.compile(r"safari/", re.IGNORECASE)),
)
_OS_RES = (
    ("Windows", re.compile(r"windows nt", re.IGNORECASE)),
    ("Android", re.compile(r"android", re.IGNORECASE)),
    ("iOS", re.compile(r"iphone|ipad|ipod|cpu os ", re.IGNORECASE)),
    ("Mac OS X", re.compile(r"mac os x|macintosh", re.IGNORECASE)),
    ("Chrome OS", re.compile(r"cros ", re.IGNORECASE)),
    ("Linux", re.compile(r"linux|ubuntu|fedora", re.IGNORECASE)),
)


def parse_user_agent(user_agent: str, bot: bool) -> tuple[int, str, str]:
    """``(device, browser_name, os_name)``.

    Uses the ``user-agents`` package when it's installed and falls back to a
    small regex table when it isn't, so a missing optional wheel degrades the
    detail rather than breaking collection.
    """
    if bot:
        return Device.BOT, Browser.UNKNOWN, OperatingSystem.UNKNOWN
    if not user_agent:
        return Device.OTHER, Browser.UNKNOWN, OperatingSystem.UNKNOWN

    if _parse_ua is not None:
        try:
            parsed = _parse_ua(user_agent)
        except Exception:  # pragma: no cover - never fail a hit over a UA string
            parsed = None
        if parsed is not None:
            if parsed.is_tablet:
                device = Device.TABLET
            elif parsed.is_mobile:
                device = Device.MOBILE
            elif parsed.is_pc:
                device = Device.DESKTOP
            else:
                device = Device.OTHER
            return (
                device,
                _clean(parsed.browser.family),
                _clean(parsed.os.family),
            )

    if _TABLET_RE.search(user_agent):
        device = Device.TABLET
    elif _MOBILE_RE.search(user_agent):
        device = Device.MOBILE
    else:
        device = Device.DESKTOP
    browser = next((name for name, rx in _BROWSER_RES if rx.search(user_agent)), Browser.UNKNOWN)
    os_name = next((name for name, rx in _OS_RES if rx.search(user_agent)), OperatingSystem.UNKNOWN)
    return device, browser, os_name


def _clean(name: str | None) -> str:
    name = (name or "").strip()
    if not name or name.lower() == "other":
        return Browser.UNKNOWN
    return name[:MAX_NAME]


# ---------------------------------------------------------------------------
# lookup tables
# ---------------------------------------------------------------------------


def lookup_id(model: type[Browser] | type[OperatingSystem], name: str) -> int:
    """Resolve a browser/OS name to its lookup row id, caching per process.

    Called once per hit in the flush thread; the cache means one query per new
    name per process for the life of the process, and browsers do not appear
    often.
    """
    name = _clean(name)
    key = (model._meta.db_table, name)
    cached = _lookup_cache.get(key)
    if cached is not None:
        return cached
    with _lookup_lock:
        cached = _lookup_cache.get(key)
        if cached is not None:
            return cached
        using = sitepulse_settings.DATABASE_ALIAS
        row, _ = model.objects.using(using).get_or_create(name=name)
        _lookup_cache[key] = row.id
        return row.id


def reset_caches() -> None:
    _bot_re.cache_clear()
    with _lookup_lock:
        _lookup_cache.clear()
    _geoip.cache_clear()


# ---------------------------------------------------------------------------
# geo
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _geoip():
    if not sitepulse_settings.GEOIP_ENABLED:
        return None
    try:
        from django.contrib.gis.geoip2 import GeoIP2

        return GeoIP2()
    except Exception:  # pragma: no cover - misconfigured GeoIP must not break ingest
        import logging

        logging.getLogger("sitepulse").warning(
            "GEOIP_ENABLED is on but GeoIP2 could not be initialised; "
            "country will be blank.", exc_info=True,
        )
        return None


def geo(ip: str) -> tuple[str, str]:
    """``(country_code, region)`` -- both blank when GeoIP is off or unresolvable."""
    reader = _geoip()
    if reader is None or not ip:
        return "", ""
    try:
        data = reader.city(ip)
    except Exception:
        return "", ""
    country = (data.get("country_code") or "")[:2].upper()
    if not sitepulse_settings.TRACK_REGION:
        return country, ""
    return country, (data.get("region") or "")[:64]
