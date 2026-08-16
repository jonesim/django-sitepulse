"""Settings for django-sitepulse.

Everything lives in one namespaced ``SITEPULSE`` dict in the project's settings.
Access it as::

    from sitepulse.conf import sitepulse_settings
    sitepulse_settings.RAW_RETENTION_DAYS

Values are resolved lazily and cached, and the cache is cleared whenever Django
emits ``setting_changed`` so ``@override_settings(SITEPULSE={...})`` works in tests.
"""

from __future__ import annotations

import re
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

#: Shipped defaults. A project's ``SITEPULSE`` dict is merged over the top of this,
#: key by key -- you only need to name the settings you want to change.
DEFAULTS: dict[str, Any] = {
    # -- master switch ----------------------------------------------------
    "ENABLED": True,
    # -- identity ---------------------------------------------------------
    # Cookieless by default (rotating daily salt, see identity.py). Turning
    # RETURNING_VISITORS on introduces a first-party cookie and, in the UK/EU,
    # a consent requirement -- read the docs before enabling it.
    "RETURNING_VISITORS": False,
    "VISITOR_COOKIE_NAME": "sp_vid",
    "VISITOR_COOKIE_DAYS": 365,
    "VISITOR_COOKIE_SAMESITE": "Lax",
    # Dotted path to ``callable(request) -> bool``. Only consulted when
    # RETURNING_VISITORS is on; return False and that request stays cookieless.
    "CONSENT_CHECK": None,
    "TRACK_AUTHENTICATED_USER_ID": False,
    "SESSION_TIMEOUT_MINUTES": 30,
    # -- what to collect --------------------------------------------------
    "TRACK_QUERY_COUNTS": True,
    "TRACK_REFERRER_PATH": False,
    "TRACK_REGION": False,
    "GEOIP_ENABLED": False,
    # -- exclusions -------------------------------------------------------
    "EXCLUDE_PATHS": [
        r"^/static/",
        r"^/media/",
        r"^/admin/",
        r"^/healthz",
        r"^/favicon\.ico$",
    ],
    "EXCLUDE_METHODS": ["HEAD", "OPTIONS"],
    "EXCLUDE_STAFF": True,
    # HTTP status codes never to record, e.g. [301, 302] to ignore redirects.
    "EXCLUDE_STATUS": [],
    # -- bots -------------------------------------------------------------
    # Flagged, never dropped. Extend rather than replace by using
    # EXTRA_BOT_UA_PATTERNS.
    "BOT_UA_PATTERNS": [
        r"bot", r"crawler", r"spider", r"slurp", r"curl/", r"wget", r"python-requests",
        r"httpx", r"go-http-client", r"okhttp", r"java/", r"libwww-perl", r"scrapy",
        r"headlesschrome", r"phantomjs", r"lighthouse", r"pingdom", r"uptimerobot",
        r"gtmetrix", r"facebookexternalhit", r"embedly", r"preview", r"monitoring",
        r"feedfetcher", r"apache-httpclient", r"axios/", r"node-fetch", r"probe",
    ],
    "EXTRA_BOT_UA_PATTERNS": [],
    # -- storage / retention ---------------------------------------------
    "RAW_RETENTION_DAYS": 90,
    "UNIQUE_VISITOR_RETENTION_DAYS": 730,
    "PARTITION_MONTHS_AHEAD": 2,
    # -- buffer -----------------------------------------------------------
    "BUFFER_MAX": 10_000,
    "FLUSH_EVERY_ROWS": 100,
    "FLUSH_EVERY_SECONDS": 5,
    # Write synchronously instead of via the background thread. Only for tests
    # and management commands -- never turn this on in a served process.
    "SYNCHRONOUS_WRITES": False,
    # -- histogram --------------------------------------------------------
    # Eight boundaries produce nine buckets: <=25, <=50, ... <=5000, >5000.
    "DURATION_BUCKETS_MS": [25, 50, 100, 250, 500, 1000, 2500, 5000],
    # -- plumbing ---------------------------------------------------------
    "DATABASE_ALIAS": "default",
    "CACHE_ALIAS": "default",
    "DASHBOARD_PERMISSION": "sitepulse.view_dashboard",
    "DASHBOARD_DEFAULT_DAYS": 30,
    # How long an on-the-fly aggregation of a not-yet-rolled-up day is reused.
    # Dashboards get refreshed; a day should not be re-scanned every time.
    "LIVE_CACHE_SECONDS": 60,
}

#: Settings whose value must be a list of regular expressions.
_REGEX_LISTS = ("EXCLUDE_PATHS", "BOT_UA_PATTERNS", "EXTRA_BOT_UA_PATTERNS")

#: Settings that must be a positive integer.
_POSITIVE_INTS = (
    "RAW_RETENTION_DAYS",
    "UNIQUE_VISITOR_RETENTION_DAYS",
    "SESSION_TIMEOUT_MINUTES",
    "BUFFER_MAX",
    "FLUSH_EVERY_ROWS",
    "FLUSH_EVERY_SECONDS",
    "PARTITION_MONTHS_AHEAD",
    "VISITOR_COOKIE_DAYS",
    "DASHBOARD_DEFAULT_DAYS",
    "LIVE_CACHE_SECONDS",
)

#: Number of histogram bucket columns on the rollup tables. Fixed by the schema:
#: len(DURATION_BUCKETS_MS) must be exactly one less than this.
BUCKET_COUNT = 9


class SitePulseSettings:
    """Lazy, validated accessor over ``settings.SITEPULSE``."""

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._cache[name]
        except KeyError:
            pass
        if name not in DEFAULTS:
            raise AttributeError(f"'{name}' is not a django-sitepulse setting")
        value = self._user_settings().get(name, DEFAULTS[name])
        value = self._validate(name, value)
        self._cache[name] = value
        return value

    @staticmethod
    def _user_settings() -> dict[str, Any]:
        from django.conf import settings as django_settings

        user = getattr(django_settings, "SITEPULSE", {})
        if not isinstance(user, dict):
            raise ImproperlyConfigured("settings.SITEPULSE must be a dict")
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            raise ImproperlyConfigured(
                "Unknown django-sitepulse setting(s): "
                + ", ".join(sorted(unknown))
                + ". Valid settings are: "
                + ", ".join(sorted(DEFAULTS))
            )
        return user

    def _validate(self, name: str, value: Any) -> Any:
        if name in _POSITIVE_INTS:
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ImproperlyConfigured(
                    f"SITEPULSE['{name}'] must be a positive integer, got {value!r}"
                )
        elif name in _REGEX_LISTS:
            if not isinstance(value, (list, tuple)):
                raise ImproperlyConfigured(f"SITEPULSE['{name}'] must be a list of regexes")
            for pattern in value:
                try:
                    re.compile(pattern)
                except re.error as exc:  # pragma: no cover - message is the point
                    raise ImproperlyConfigured(
                        f"SITEPULSE['{name}'] contains an invalid regex {pattern!r}: {exc}"
                    ) from exc
        elif name == "DURATION_BUCKETS_MS":
            if not isinstance(value, (list, tuple)) or len(value) != BUCKET_COUNT - 1:
                raise ImproperlyConfigured(
                    f"SITEPULSE['DURATION_BUCKETS_MS'] must contain exactly "
                    f"{BUCKET_COUNT - 1} boundaries (the schema has {BUCKET_COUNT} "
                    f"bucket columns), got {len(value) if hasattr(value, '__len__') else value!r}"
                )
            if list(value) != sorted(value) or len(set(value)) != len(value):
                raise ImproperlyConfigured(
                    "SITEPULSE['DURATION_BUCKETS_MS'] must be strictly ascending"
                )
            value = list(value)
        return value

    def reset(self) -> None:
        self._cache.clear()

    def as_dict(self) -> dict[str, Any]:
        """Every resolved setting -- used by the dashboard's diagnostics page."""
        return {name: getattr(self, name) for name in sorted(DEFAULTS)}


sitepulse_settings = SitePulseSettings()


@receiver(setting_changed)
def _reset_on_setting_changed(sender, setting, **kwargs):  # pragma: no cover - trivial
    if setting == "SITEPULSE":
        sitepulse_settings.reset()
        # Compiled exclusion/bot patterns are derived from settings.
        from sitepulse import enrich, middleware, normalise

        normalise.reset_caches()
        enrich.reset_caches()
        middleware.reset_caches()
