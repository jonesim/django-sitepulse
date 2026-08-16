"""The collector.

It sees every server-rendered request, needs no JavaScript, cannot be blocked by
an ad blocker, and is the only thing that can measure server-side timing.

Structurally the one thing to know is that all of the work happens *after*
``get_response(request)``: ``request.resolver_match`` is only populated once URL
resolution has run, so a middleware that inspects it on the way in gets ``None``.
The handler sets it on the same request object before invoking the view, so
reading it on the way out is both correct and documented.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.core.exceptions import MiddlewareNotUsed
from django.db import connections
from django.utils import timezone
from django.utils.module_loading import import_string

from . import normalise
from .buffer import PendingHit, buffer
from .conf import sitepulse_settings
from .identity import new_cookie_value
from .models import Method

logger = logging.getLogger("sitepulse")

MAX_VIEW_NAME = 255


class QueryCounter:
    """Counts queries and database time for one request, on one thread.

    ``connection.queries`` is the obvious-looking approach and the wrong one: it
    is only populated when ``DEBUG=True`` and it is capped at the last 9,000
    queries per connection, so it is both unavailable in production and lossy.
    ``connection.execute_wrapper()`` is the documented instrumentation API, works
    regardless of DEBUG, and is scoped to the ``with`` block on the calling
    thread -- which is exactly one request.
    """

    __slots__ = ("count", "seconds", "_stack")

    def __init__(self) -> None:
        self.count = 0
        self.seconds = 0.0
        self._stack = None

    def __call__(self, execute, sql, params, many, context):
        start = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            self.count += 1
            self.seconds += time.perf_counter() - start

    def __enter__(self):
        from contextlib import ExitStack

        from django.conf import settings as django_settings

        self._stack = ExitStack()
        for alias in django_settings.DATABASES:
            # Creating the connection object is lazy -- this does not connect.
            self._stack.enter_context(connections[alias].execute_wrapper(self))
        return self

    def __exit__(self, *exc_info):
        stack, self._stack = self._stack, None
        if stack is not None:
            stack.close()
        return False


class _NullCounter:
    count = None
    seconds = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@lru_cache(maxsize=1)
def _consent_check():
    path = sitepulse_settings.CONSENT_CHECK
    return import_string(path) if path else None


class AnalyticsMiddleware:
    sync_capable = True
    #: Django defaults this to False, and leaving it there forces every request
    #: in an ASGI deployment through a thread pool. There is no genuinely
    #: blocking work here, so the async path is nearly free.
    async_capable = True

    def __init__(self, get_response):
        if not sitepulse_settings.ENABLED:
            raise MiddlewareNotUsed
        self.get_response = get_response
        self.async_mode = iscoroutinefunction(get_response)
        if self.async_mode:
            markcoroutinefunction(self)

    # -- entry points -----------------------------------------------------

    def __call__(self, request):
        if self.async_mode:
            return self.__acall__(request)
        start = time.perf_counter()
        counter = self._counter()
        with counter:
            response = self.get_response(request)
        self._finish(request, response, start, counter)
        return response

    async def __acall__(self, request):
        start = time.perf_counter()
        # Query counting is skipped under ASGI: execute_wrapper is bound to the
        # connection object of the calling thread, and ORM calls in an async
        # view run on a different one, so the count would be silently wrong.
        response = await self.get_response(request)
        self._finish(request, response, start, _NullCounter())
        return response

    def _counter(self):
        return QueryCounter() if sitepulse_settings.TRACK_QUERY_COUNTS else _NullCounter()

    # -- recording --------------------------------------------------------

    def _finish(self, request, response, start, counter) -> None:
        try:
            duration_ms = int((time.perf_counter() - start) * 1000)
            if self._skip(request, response):
                return
            hit = self._build(request, response, duration_ms, counter)
            if hit is None:
                return
            buffer.add(hit)
        except Exception:  # pragma: no cover - analytics must never break a request
            logger.exception("sitepulse: failed to record a hit")

    def _skip(self, request, response) -> bool:
        if getattr(request, "sitepulse_skip", False):
            return True
        if request.method in sitepulse_settings.EXCLUDE_METHODS:
            return True
        if normalise.is_excluded(request.path):
            return True
        match = getattr(request, "resolver_match", None)
        if match is not None and (match.app_name == "sitepulse" or match.namespace == "sitepulse"):
            # Never let the dashboard show up in its own numbers.
            return True
        if sitepulse_settings.EXCLUDE_STAFF:
            user = getattr(request, "user", None)
            if user is not None and getattr(user, "is_staff", False):
                return True
        if response.status_code in sitepulse_settings.EXCLUDE_STATUS:
            return True
        return False

    def _build(self, request, response, duration_ms, counter) -> PendingHit | None:
        match = getattr(request, "resolver_match", None)
        host = ""
        try:
            host = request.get_host().split(":", 1)[0].lower()
        except Exception:  # pragma: no cover - DisallowedHost
            pass

        referrer_host, referrer_path = normalise.referrer_parts(
            request.META.get("HTTP_REFERER"), normalise.own_hosts(request)
        )
        utm_source, utm_medium, utm_campaign = normalise.utm_params(request.GET)

        user_id = None
        if sitepulse_settings.TRACK_AUTHENTICATED_USER_ID:
            user = getattr(request, "user", None)
            if user is not None and getattr(user, "is_authenticated", False):
                user_id = user.pk

        return PendingHit(
            ts=timezone.now(),
            ip=self._ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "") or "",
            host=host,
            path=normalise.normalise_path(request.path),
            route=normalise.normalise_route(getattr(match, "route", None)),
            view_name=(getattr(match, "view_name", "") or "")[:MAX_VIEW_NAME],
            method=Method.from_name(request.method),
            status=response.status_code,
            duration_ms=duration_ms,
            query_count=counter.count,
            query_ms=None if counter.seconds is None else int(counter.seconds * 1000),
            referrer_host=referrer_host,
            referrer_path=referrer_path,
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
            user_id=user_id,
            cookie_id=self._cookie_id(request, response),
        )

    @staticmethod
    def _ip(request) -> str:
        from .identity import client_ip

        return client_ip(request)

    def _cookie_id(self, request, response) -> str:
        """The opt-in returning-visitor cookie (§3, open decision A).

        Off by default. When it is on, a ``CONSENT_CHECK`` callable decides per
        request whether this visitor has consented; without consent the request
        falls back to the cookieless hash, so a mixed-consent site still gets
        complete traffic numbers.
        """
        if not sitepulse_settings.RETURNING_VISITORS:
            return ""
        check = _consent_check()
        if check is not None and not check(request):
            return ""
        name = sitepulse_settings.VISITOR_COOKIE_NAME
        value = request.COOKIES.get(name) or ""
        if not value or len(value) != 32 or not _is_hex(value):
            value = new_cookie_value()
            try:
                response.set_cookie(
                    name,
                    value,
                    max_age=sitepulse_settings.VISITOR_COOKIE_DAYS * 86400,
                    samesite=sitepulse_settings.VISITOR_COOKIE_SAMESITE,
                    secure=request.is_secure(),
                    httponly=True,
                )
            except Exception:  # pragma: no cover - streaming responses etc.
                return ""
        return value


def reset_caches() -> None:
    _consent_check.cache_clear()


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
