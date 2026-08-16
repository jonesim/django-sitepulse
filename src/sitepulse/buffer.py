"""The write path.

    request  ->  PendingHit dataclass  ->  bounded deque  ->  background thread
                                                              -> enrich
                                                              -> bulk_create

The request path does no I/O at all: it builds a plain object and appends it to a
deque, which is nanoseconds. Everything expensive -- hashing, user-agent parsing,
geo lookup, session assignment, the actual insert -- happens in the flush thread
on a whole batch at a time.

Two properties this file exists to guarantee:

* analytics can never take the site down. The deque is bounded, overflow drops
  the *oldest* rows and increments a counter the dashboard shows, and every
  exception in the flush thread is swallowed and logged.
* analytics never adds a synchronous database write to the request, because that
  write lands squarely in your p99.

The multi-process caveat, stated plainly: each Gunicorn/uWSGI worker has its own
buffer and its own flush thread, so N workers means up to N ``bulk_create`` calls
every ``FLUSH_EVERY_SECONDS``. At the volumes this package targets that is a
handful of batched inserts per second, which is fine. It is also exactly the part
that stops being fine if you get 50x busier -- at which point swap this module
for a queue and nothing else moves.
"""

from __future__ import annotations

import atexit
import dataclasses
import logging
import os
import signal
import threading
from collections import deque
from datetime import datetime
from typing import Any

from django.db import close_old_connections
from django.utils import timezone

from .conf import sitepulse_settings

logger = logging.getLogger("sitepulse")


@dataclasses.dataclass(slots=True)
class PendingHit:
    """What the middleware builds. Not a model instance; never touches the ORM.

    ``ip`` and ``user_agent`` are the only PII in the system and they exist only
    here, in memory, for at most ``FLUSH_EVERY_SECONDS``. They are consumed by
    enrichment and never written anywhere.
    """

    ts: datetime
    ip: str
    user_agent: str
    host: str
    path: str
    route: str
    view_name: str
    method: int
    status: int
    duration_ms: int
    query_count: int | None = None
    query_ms: int | None = None
    referrer_host: str = ""
    referrer_path: str = ""
    utm_source: str = ""
    utm_medium: str = ""
    utm_campaign: str = ""
    user_id: int | None = None
    screen_w: int | None = None
    props: dict[str, Any] | None = None
    cookie_id: str = ""

    # filled in by the flush thread
    visitor_hash: bytes = b""
    session_id: bytes = b""
    is_new_session: bool = False


class HitBuffer:
    """A bounded deque plus the daemon thread that drains it."""

    def __init__(self) -> None:
        # Unbounded deque with the bound enforced in add(), so that changing
        # BUFFER_MAX (including via override_settings) takes effect immediately.
        self._queue: deque[PendingHit] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._owner_pid: int | None = None
        self._shutdown_hooked = False
        self.dropped = 0
        self.write_errors = 0

    # -- producer side ----------------------------------------------------

    def add(self, hit: PendingHit) -> None:
        if sitepulse_settings.SYNCHRONOUS_WRITES:
            self._write([hit])
            return
        queue = self._queue
        while len(queue) >= sitepulse_settings.BUFFER_MAX:
            # Drop the oldest: recent data is the data anyone is looking at, and
            # a visible gap beats an OOM.
            try:
                queue.popleft()
            except IndexError:  # pragma: no cover - another thread got there first
                break
            self.dropped += 1
        queue.append(hit)
        self._ensure_thread()
        if len(queue) >= sitepulse_settings.FLUSH_EVERY_ROWS:
            self._wake.set()

    # -- thread lifecycle -------------------------------------------------

    def _ensure_thread(self) -> None:
        """Start the flush thread lazily, and restart it after a fork.

        Lazily rather than in ``AppConfig.ready()`` because a thread started
        before Gunicorn forks (``--preload``) does not survive into the workers,
        and because ``manage.py migrate`` has no business running one.
        """
        pid = os.getpid()
        thread = self._thread
        if thread is not None and thread.is_alive() and self._owner_pid == pid:
            return
        with self._lock:
            thread = self._thread
            if thread is not None and thread.is_alive() and self._owner_pid == pid:
                return
            if self._owner_pid not in (None, pid):
                # Inherited a parent's queue contents across a fork; the parent
                # will write them, so don't write them twice.
                self._queue.clear()
            self._stopping.clear()
            self._owner_pid = pid
            self._thread = threading.Thread(
                target=self._run, name="sitepulse-flush", daemon=True
            )
            self._thread.start()
            self._install_shutdown_hooks()

    def _install_shutdown_hooks(self) -> None:
        if self._shutdown_hooked:
            return
        self._shutdown_hooked = True
        atexit.register(self.shutdown)
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, getattr(signal, "SIGINT", None)):
                if sig is None:
                    continue
                try:
                    previous = signal.getsignal(sig)
                    signal.signal(sig, self._make_handler(previous))
                except (ValueError, OSError):  # pragma: no cover - not the main thread
                    pass

    def _make_handler(self, previous):
        def handler(signum, frame):
            try:
                self.shutdown()
            finally:
                if callable(previous):
                    previous(signum, frame)
                elif previous == signal.SIG_DFL:
                    signal.signal(signum, signal.SIG_DFL)
                    os.kill(os.getpid(), signum)

        return handler

    def shutdown(self) -> None:
        """Flush what's buffered. Registered with ``atexit`` and on SIGTERM, so a
        graceful deploy doesn't lose the last few seconds of traffic."""
        self._stopping.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        self.flush()

    # -- consumer side ----------------------------------------------------

    def _run(self) -> None:
        interval = sitepulse_settings.FLUSH_EVERY_SECONDS
        while not self._stopping.is_set():
            self._wake.wait(timeout=interval)
            self._wake.clear()
            try:
                self.flush()
            except Exception:  # pragma: no cover - flush already swallows
                logger.exception("sitepulse: flush loop error")
            finally:
                # This thread outlives any request, so it has to honour
                # CONN_MAX_AGE itself or it holds a connection open forever.
                # Only here: on the synchronous path the request cycle already
                # owns the connection's lifetime.
                close_old_connections()
        self.flush()

    def _drain(self) -> list[PendingHit]:
        queue = self._queue
        batch: list[PendingHit] = []
        try:
            while True:
                batch.append(queue.popleft())
        except IndexError:
            pass
        return batch

    def flush(self) -> int:
        """Write everything currently buffered. Returns the number of rows written."""
        batch = self._drain()
        if not batch:
            self._record_health()
            return 0
        return self._write(batch)

    def _write(self, batch: list[PendingHit]) -> int:
        from .models import Hit

        try:
            rows = build_rows(batch)
        except Exception:
            logger.exception("sitepulse: could not build %d hits", len(batch))
            self.write_errors += 1
            self._record_health()
            return 0

        using = sitepulse_settings.DATABASE_ALIAS
        try:
            Hit.objects.using(using).bulk_create(rows, batch_size=500)
        except Exception:
            # A database blip loses a batch of analytics. It does not raise into
            # a user request, and it does not retry into a queue that then grows
            # without bound.
            logger.exception("sitepulse: dropped %d hits on write", len(rows))
            self.write_errors += 1
            self._record_health()
            return 0

        self._record_health()
        return len(rows)

    def _record_health(self) -> None:
        """Persist drop/error counters so the dashboard can show them.

        Only touches the database when something actually went wrong, so the
        happy path costs one attribute check.
        """
        if not (self.dropped or self.write_errors):
            return
        dropped, self.dropped = self.dropped, 0
        errors, self.write_errors = self.write_errors, 0
        try:
            from django.db.models import F

            from .models import IngestHealth

            using = sitepulse_settings.DATABASE_ALIAS
            today = timezone.localdate()
            updated = IngestHealth.objects.using(using).filter(date=today).update(
                dropped=F("dropped") + dropped, write_errors=F("write_errors") + errors
            )
            if not updated:
                IngestHealth.objects.using(using).create(
                    date=today, dropped=dropped, write_errors=errors
                )
        except Exception:  # pragma: no cover
            logger.exception("sitepulse: could not record ingest health")


def build_rows(batch: list[PendingHit]) -> list:
    """Enrich a batch and turn it into unsaved ``Hit`` instances."""
    from . import enrich, identity
    from .models import Browser, Hit, OperatingSystem

    track_region = sitepulse_settings.TRACK_REGION
    enriched = []

    for pending in batch:
        bot = enrich.is_bot(pending.user_agent)
        device, browser_name, os_name = enrich.parse_user_agent(pending.user_agent, bot)
        country, region = enrich.geo(pending.ip)
        pending.props = pending.props or None
        if pending.cookie_id:
            pending.visitor_hash = identity.cookie_visitor_hash(pending.cookie_id)
        else:
            pending.visitor_hash = identity.visitor_hash(
                pending.host, pending.ip, pending.user_agent, pending.ts.date()
            )
        # The IP and user-agent have now done their only job. Clear them so that
        # nothing downstream -- including a traceback -- can carry them further.
        pending.ip = ""
        pending.user_agent = ""
        enriched.append(
            (bot, device, browser_name, os_name, country, region if track_region else "")
        )

    identity.assign_sessions(batch)

    rows = []
    for pending, (bot, device, browser_name, os_name, country, region) in zip(
        batch, enriched, strict=True
    ):
        rows.append(
            Hit(
                ts=pending.ts,
                visitor_hash=pending.visitor_hash,
                session_id=pending.session_id,
                is_new_session=pending.is_new_session,
                path=pending.path,
                route=pending.route,
                view_name=pending.view_name,
                method=pending.method,
                status=pending.status,
                duration_ms=pending.duration_ms,
                query_count=pending.query_count,
                query_ms=pending.query_ms,
                referrer_host=pending.referrer_host,
                referrer_path=pending.referrer_path,
                utm_source=pending.utm_source,
                utm_medium=pending.utm_medium,
                utm_campaign=pending.utm_campaign,
                country=country,
                region=region,
                device=device,
                browser_id=enrich.lookup_id(Browser, browser_name),
                os_id=enrich.lookup_id(OperatingSystem, os_name),
                is_bot=bot,
                user_id=pending.user_id,
                screen_w=pending.screen_w,
                props=pending.props,
            )
        )
    return rows


#: The process-wide buffer. One per worker process, by design.
buffer = HitBuffer()
