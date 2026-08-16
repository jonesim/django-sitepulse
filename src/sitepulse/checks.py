"""System checks.

These exist because every one of them is a mistake that produces plausible-looking
but wrong numbers, which is worse than an error.
"""

from __future__ import annotations

from django.core.checks import Error, Warning, register

from .conf import sitepulse_settings

MIDDLEWARE_PATH = "sitepulse.middleware.AnalyticsMiddleware"


@register()
def check_sitepulse(app_configs, **kwargs):
    from django.conf import settings

    issues = []

    if not sitepulse_settings.ENABLED:
        return issues

    if MIDDLEWARE_PATH not in settings.MIDDLEWARE:
        issues.append(
            Warning(
                "sitepulse is installed but AnalyticsMiddleware is not in MIDDLEWARE, "
                "so nothing is being collected.",
                hint=f"Add '{MIDDLEWARE_PATH}' to MIDDLEWARE, as late as possible so it "
                     "times the work the other middleware do.",
                id="sitepulse.W001",
            )
        )

    alias = sitepulse_settings.DATABASE_ALIAS
    if alias not in settings.DATABASES:
        issues.append(
            Error(
                f"SITEPULSE['DATABASE_ALIAS'] is {alias!r}, which is not in DATABASES.",
                id="sitepulse.E001",
            )
        )

    cache_alias = sitepulse_settings.CACHE_ALIAS
    caches = getattr(settings, "CACHES", {})
    if cache_alias not in caches:
        issues.append(
            Error(
                f"SITEPULSE['CACHE_ALIAS'] is {cache_alias!r}, which is not in CACHES.",
                id="sitepulse.E002",
            )
        )
    else:
        backend = caches[cache_alias].get("BACKEND", "")
        if "locmem" in backend or "dummy" in backend:
            issues.append(
                Warning(
                    f"sitepulse is using the {cache_alias!r} cache ({backend}) for session "
                    "assembly. A per-process or no-op cache means every worker sees its own "
                    "sessions, so session, entry, exit and bounce counts will be inflated.",
                    hint="Point SITEPULSE['CACHE_ALIAS'] at a shared cache (Redis, Memcached, "
                         "or the database cache backend).",
                    id="sitepulse.W002",
                )
            )

    if sitepulse_settings.RETURNING_VISITORS:
        if not sitepulse_settings.CONSENT_CHECK:
            issues.append(
                Warning(
                    "SITEPULSE['RETURNING_VISITORS'] is on, which sets a first-party "
                    "cookie, but no CONSENT_CHECK is configured -- the cookie will be set "
                    "for every visitor. In the UK/EU that needs consent.",
                    hint="Set SITEPULSE['CONSENT_CHECK'] to a dotted path to "
                         "callable(request) -> bool, or turn RETURNING_VISITORS off to stay "
                         "cookieless.",
                    id="sitepulse.W003",
                )
            )
        else:
            try:
                from django.utils.module_loading import import_string

                import_string(sitepulse_settings.CONSENT_CHECK)
            except ImportError as exc:
                issues.append(
                    Error(
                        f"SITEPULSE['CONSENT_CHECK'] could not be imported: {exc}",
                        id="sitepulse.E003",
                    )
                )

    return issues


@register(deploy=True)
def check_sitepulse_deployment(app_configs, **kwargs):
    from django.db import connections

    issues = []
    if not sitepulse_settings.ENABLED:
        return issues

    if sitepulse_settings.SYNCHRONOUS_WRITES:
        issues.append(
            Warning(
                "SITEPULSE['SYNCHRONOUS_WRITES'] is on, which writes every hit inside the "
                "request. That insert lands in your p99.",
                hint="It exists for tests and management commands. Turn it off in production.",
                id="sitepulse.W004",
            )
        )

    alias = sitepulse_settings.DATABASE_ALIAS
    if alias in connections and connections[alias].vendor != "postgresql":
        issues.append(
            Warning(
                f"sitepulse is running on {connections[alias].vendor}. It works, but "
                "partitioned retention, jsonb props and exact percentiles are "
                "PostgreSQL-only; pruning falls back to chunked deletes.",
                id="sitepulse.W005",
            )
        )

    return issues
