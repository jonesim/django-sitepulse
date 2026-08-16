"""Middleware, buffer, normalisation, identity, enrichment."""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone

from sitepulse import identity, normalise
from sitepulse.buffer import PendingHit, buffer, build_rows
from sitepulse.enrich import is_bot, parse_user_agent
from sitepulse.models import Device, Hit, Method, Salt

pytestmark = pytest.mark.django_db

CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/orders/1/", "/orders/1"),
        ("/orders//1", "/orders/1"),
        ("/", "/"),
        ("", "/"),
        ("orders", "/orders"),
        ("/a?b=c", "/a"),
        ("/x" * 400, ("/x" * 400)[:255]),
    ],
)
def test_path_normalisation(raw, expected):
    assert normalise.normalise_path(raw) == expected


def test_route_of_an_unresolved_request_is_blank():
    # 404s and short-circuited middleware have no resolver_match at all.
    assert normalise.normalise_route(None) == ""


def test_self_referrals_are_not_a_traffic_source():
    own = frozenset({"example.com"})
    assert normalise.referrer_parts("https://example.com/a", own) == ("", "")
    assert normalise.referrer_parts("https://news.ycombinator.com/x", own)[0] == (
        "news.ycombinator.com"
    )
    assert normalise.referrer_parts(None, own) == ("", "")


def test_referrer_path_is_off_by_default():
    host, path = normalise.referrer_parts("https://other.example/some/page", frozenset())
    assert (host, path) == ("other.example", "")


@override_settings(SITEPULSE={"SYNCHRONOUS_WRITES": True, "TRACK_REFERRER_PATH": True})
def test_referrer_path_can_be_turned_on():
    normalise.reset_caches()
    assert normalise.referrer_parts("https://other.example/some/page", frozenset()) == (
        "other.example", "/some/page",
    )


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def test_visitor_hash_is_stable_within_a_day_and_16_bytes():
    first = identity.visitor_hash("example.com", "1.2.3.4", CHROME)
    second = identity.visitor_hash("example.com", "1.2.3.4", CHROME)
    assert first == second
    assert len(first) == 16


def test_visitor_hash_changes_with_the_salt():
    today = timezone.localdate()
    first = identity.visitor_hash("example.com", "1.2.3.4", CHROME)
    Salt.objects.all().delete()
    identity.reset_salt_cache()
    second = identity.visitor_hash("example.com", "1.2.3.4", CHROME, today)
    assert first != second


def test_creating_a_new_salt_destroys_every_older_one():
    yesterday = timezone.localdate() - timedelta(days=1)
    Salt.objects.create(date=yesterday, value=b"old")
    identity.reset_salt_cache()
    identity.current_salt(timezone.localdate())
    assert not Salt.objects.filter(date=yesterday).exists()


def test_sessions_continue_within_the_timeout_and_restart_after_it():
    visitor = os.urandom(16)
    now = timezone.now()
    hits = [
        _pending(visitor, now),
        _pending(visitor, now + timedelta(minutes=5)),
        _pending(visitor, now + timedelta(minutes=90)),
    ]
    identity.assign_sessions(hits)
    assert hits[0].session_id == hits[1].session_id
    assert hits[0].is_new_session and not hits[1].is_new_session
    assert hits[2].session_id != hits[0].session_id
    assert hits[2].is_new_session


def _pending(visitor, ts):
    hit = PendingHit(
        ts=ts, ip="", user_agent="", host="example.com", path="/", route="/",
        view_name="home", method=Method.GET, status=200, duration_ms=1,
    )
    hit.visitor_hash = visitor
    return hit


# ---------------------------------------------------------------------------
# enrichment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ua,expected",
    [
        (CHROME, False),
        ("Googlebot/2.1 (+http://www.google.com/bot.html)", True),
        ("python-requests/2.31", True),
        ("", True),
    ],
)
def test_bot_detection(ua, expected):
    assert is_bot(ua) is expected


def test_bots_are_flagged_never_dropped():
    hit = PendingHit(
        ts=timezone.now(), ip="1.2.3.4", user_agent="Googlebot/2.1", host="example.com",
        path="/", route="/", view_name="home", method=Method.GET, status=200, duration_ms=5,
    )
    rows = build_rows([hit])
    assert len(rows) == 1
    assert rows[0].is_bot is True
    assert rows[0].device == Device.BOT


def test_user_agent_parsing():
    device, browser, os_name = parse_user_agent(CHROME, bot=False)
    assert device == Device.DESKTOP
    assert browser == "Chrome"
    assert "Windows" in os_name


def test_raw_ip_and_user_agent_never_reach_the_database():
    hit = PendingHit(
        ts=timezone.now(), ip="203.0.113.9", user_agent=CHROME, host="example.com",
        path="/", route="/", view_name="home", method=Method.GET, status=200, duration_ms=5,
    )
    rows = build_rows([hit])
    stored = " ".join(str(value) for value in rows[0].__dict__.values())
    assert "203.0.113.9" not in stored
    assert "Chrome/125" not in stored
    # ...and they are cleared from the in-memory object too.
    assert hit.ip == "" and hit.user_agent == ""


# ---------------------------------------------------------------------------
# buffer
# ---------------------------------------------------------------------------


@override_settings(SITEPULSE={"SYNCHRONOUS_WRITES": False, "BUFFER_MAX": 3})
def test_the_buffer_is_bounded_and_counts_what_it_drops(monkeypatch):
    # No flush thread: this is about what add() does when the queue is full.
    monkeypatch.setattr(buffer, "_ensure_thread", lambda: None)
    hits = [_pending(os.urandom(16), timezone.now()) for _ in range(10)]
    for hit in hits:
        buffer.add(hit)
    assert len(buffer._queue) == 3
    assert buffer.dropped == 7
    # The *oldest* go: what is left is the most recent three.
    assert list(buffer._queue) == hits[-3:]


def test_a_write_failure_loses_the_batch_without_raising(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr("django.db.models.query.QuerySet.bulk_create", explode)
    written = buffer._write([_pending(os.urandom(16), timezone.now())])
    assert written == 0
    assert buffer.write_errors == 0  # flushed into IngestHealth
    from sitepulse.models import IngestHealth

    assert IngestHealth.objects.get(date=timezone.localdate()).write_errors == 1


# ---------------------------------------------------------------------------
# middleware
# ---------------------------------------------------------------------------


def test_a_request_records_the_route_not_just_the_path(client):
    client.get("/orders/8814/", HTTP_USER_AGENT=CHROME)
    hit = Hit.objects.get()
    assert hit.path == "/orders/8814"
    assert hit.route == "/orders/<int:pk>"
    assert hit.view_name == "order_detail"
    assert hit.status == 200
    assert hit.method == Method.GET


def test_included_urlconfs_keep_the_full_route(client):
    client.get("/orders/1/lines/", HTTP_USER_AGENT=CHROME)
    assert Hit.objects.get().route == "/orders/<int:pk>/lines"


def test_a_404_is_recorded_with_a_blank_route(client):
    client.get("/nothing-here/", HTTP_USER_AGENT=CHROME)
    hit = Hit.objects.get()
    assert hit.status == 404
    assert hit.route == ""


def test_query_counts_are_captured_without_debug(client, settings):
    settings.DEBUG = False
    client.get("/", HTTP_USER_AGENT=CHROME)
    hit = Hit.objects.get()
    assert hit.query_count is not None


def test_excluded_paths_are_not_recorded(client):
    client.get("/static/app.css", HTTP_USER_AGENT=CHROME)
    assert not Hit.objects.exists()


def test_the_dashboard_stays_out_of_its_own_numbers(client, admin_user):
    client.force_login(admin_user)
    client.get("/analytics/", HTTP_USER_AGENT=CHROME)
    assert not Hit.objects.filter(path__startswith="/analytics").exists()


@override_settings(SITEPULSE={"SYNCHRONOUS_WRITES": True, "EXCLUDE_STAFF": True})
def test_staff_requests_can_be_excluded(client):
    staff = User.objects.create_user("staff", is_staff=True)
    client.force_login(staff)
    client.get("/", HTTP_USER_AGENT=CHROME)
    assert not Hit.objects.exists()


@override_settings(SITEPULSE={"SYNCHRONOUS_WRITES": True, "TRACK_AUTHENTICATED_USER_ID": True,
                              "EXCLUDE_STAFF": False})
def test_user_id_is_recorded_only_when_opted_in(client):
    user = User.objects.create_user("someone")
    client.force_login(user)
    client.get("/", HTTP_USER_AGENT=CHROME)
    assert Hit.objects.get().user_id == user.pk


def test_user_id_is_off_by_default(client):
    user = User.objects.create_user("someone")
    client.force_login(user)
    client.get("/", HTTP_USER_AGENT=CHROME)
    assert Hit.objects.get().user_id is None


def test_utm_parameters_are_promoted_out_of_the_query_string(client):
    client.get("/?utm_source=newsletter&utm_medium=email&x=1", HTTP_USER_AGENT=CHROME)
    hit = Hit.objects.get()
    assert hit.utm_source == "newsletter"
    assert hit.utm_medium == "email"
    assert hit.path == "/"


def test_the_middleware_declares_itself_async_capable():
    """An async_capable=False middleware forces every request in an ASGI
    deployment through a thread pool, which is an expensive thing to leave out."""
    from sitepulse.middleware import AnalyticsMiddleware

    assert AnalyticsMiddleware.async_capable is True
    assert AnalyticsMiddleware.sync_capable is True


@override_settings(SITEPULSE={"SYNCHRONOUS_WRITES": False, "EXCLUDE_STAFF": False})
def test_the_async_path_records_a_hit_and_touches_no_database(async_client):
    """Under ASGI the request path must stay free of ORM work entirely -- any
    database access in an async context raises SynchronousOnlyOperation, and the
    only reason this passes is that everything is deferred to the flush thread."""
    import asyncio

    response = asyncio.run(async_client.get("/", headers={"user-agent": CHROME}))
    assert response.status_code == 200
    assert not Hit.objects.exists()          # nothing written yet: it is buffered

    buffer.flush()
    hit = Hit.objects.get()
    assert hit.path == "/"
    # Query counting is deliberately skipped under ASGI -- execute_wrapper is
    # bound to the calling thread's connection and ORM work runs on another.
    assert hit.query_count is None


def test_an_exception_in_recording_never_breaks_the_request(client, monkeypatch):
    monkeypatch.setattr(
        "sitepulse.middleware.AnalyticsMiddleware._build",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    response = client.get("/", HTTP_USER_AGENT=CHROME)
    assert response.status_code == 200
    assert not Hit.objects.exists()
