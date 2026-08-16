"""Helpers for making hits without going through the middleware."""

from __future__ import annotations

import os
from datetime import timedelta

from django.utils import timezone

from sitepulse.enrich import lookup_id
from sitepulse.models import Browser, Device, Hit, Method, OperatingSystem


def make_hit(**kwargs) -> Hit:
    """A saved Hit with sensible defaults, overridable field by field."""
    now = kwargs.pop("ts", None) or timezone.now()
    visitor = kwargs.pop("visitor_hash", None) or os.urandom(16)
    session = kwargs.pop("session_id", None) or os.urandom(16)
    defaults = {
        "ts": now,
        "visitor_hash": visitor,
        "session_id": session,
        "is_new_session": True,
        "path": "/",
        "route": "/",
        "view_name": "home",
        "method": Method.GET,
        "status": 200,
        "duration_ms": 30,
        "query_count": 3,
        "query_ms": 5,
        "referrer_host": "",
        "device": Device.DESKTOP,
        "browser_id": lookup_id(Browser, "Firefox"),
        "os_id": lookup_id(OperatingSystem, "Linux"),
        "is_bot": False,
    }
    defaults.update(kwargs)
    return Hit.objects.create(**defaults)


def make_session(paths, start=None, visitor=None, gap_minutes=1, **kwargs) -> list[Hit]:
    """A run of hits sharing one session id, one path each, a minute apart."""
    start = start or timezone.now()
    visitor = visitor or os.urandom(16)
    session = os.urandom(16)
    route = kwargs.pop("route", None)
    hits = []
    for index, path in enumerate(paths):
        hits.append(
            make_hit(
                ts=start + timedelta(minutes=index * gap_minutes),
                visitor_hash=visitor,
                session_id=session,
                is_new_session=index == 0,
                path=path,
                route=path if route is None else route,
                **kwargs,
            )
        )
    return hits
