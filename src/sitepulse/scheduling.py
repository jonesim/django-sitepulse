"""Making the nightly job safe to schedule more than once.

There is deliberately no Celery, RQ or APScheduler anywhere in this package --
scheduling is the host project's business, and the design's "you do not need a
queue" argument would be undermined by shipping one. What the package owes a
scheduler is a command that cannot hurt itself if it gets started twice, which is
what this module provides.

Beat schedulers double-fire during rolling deploys, cron runs on two boxes
because someone forgot to remove the entry from one of them, and people run
things by hand while the timer is already going. ``rollup_day`` deletes and
rewrites a day inside a transaction, so two concurrent runs can deadlock or
collide on a grain's unique constraint. One lock removes the whole class of
problem.

The lock lives in the database rather than the cache, for the same reason the
salt does: it has to hold across processes and survive a cache restart. It is a
row in the ``State`` table whose primary key does the mutual exclusion, so it
needs no backend-specific locking and works identically on SQLite and PostgreSQL.
"""

from __future__ import annotations

import logging
import os
import socket
from contextlib import contextmanager
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .conf import sitepulse_settings
from .models import State

logger = logging.getLogger("sitepulse")

LOCK_KEY = "nightly_lock"

#: How long a held lock is believed before it is treated as debris from a
#: process that was killed mid-run. Long enough to cover a big backfill, short
#: enough that a crash costs one night rather than silently stopping the rollups
#: until someone notices months later.
DEFAULT_STALE_AFTER = timedelta(hours=6)


class AlreadyRunning(RuntimeError):
    """Another process holds the nightly lock."""

    def __init__(self, holder: dict, since):
        self.holder = holder
        self.since = since
        super().__init__(
            f"another sitepulse_nightly is running "
            f"(host {holder.get('host', '?')}, pid {holder.get('pid', '?')}, "
            f"since {since:%Y-%m-%d %H:%M:%S})"
        )


def _holder() -> dict:
    return {
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "started": timezone.now().isoformat(),
    }


@contextmanager
def nightly_lock(stale_after: timedelta = DEFAULT_STALE_AFTER):
    """Hold the nightly lock for the duration of the block.

    Raises :class:`AlreadyRunning` if someone else holds it and their claim is
    still fresh. A claim older than ``stale_after`` is taken over, because the
    alternative -- a crashed run blocking every future one -- is worse than the
    small chance of overlapping with a genuinely very slow one.
    """
    using = sitepulse_settings.DATABASE_ALIAS
    acquired = _acquire(using, stale_after)
    if not acquired:
        row = State.objects.using(using).filter(key=LOCK_KEY).first()
        raise AlreadyRunning(row.value if row else {}, row.updated if row else timezone.now())
    try:
        yield
    finally:
        State.objects.using(using).filter(key=LOCK_KEY).delete()


def _acquire(using: str, stale_after: timedelta) -> bool:
    try:
        # The primary key is what makes this atomic; no backend needs to support
        # SELECT ... FOR UPDATE for it to work. The savepoint is so that the
        # IntegrityError does not poison an outer transaction.
        with transaction.atomic(using=using):
            State.objects.using(using).create(key=LOCK_KEY, value=_holder())
        return True
    except IntegrityError:
        pass

    row = State.objects.using(using).filter(key=LOCK_KEY).first()
    if row is None:  # released between the create and the read
        return _acquire(using, stale_after)
    if timezone.now() - row.updated < stale_after:
        return False

    logger.warning(
        "sitepulse: taking over a nightly lock last touched at %s by %s -- "
        "the previous run was almost certainly killed",
        row.updated, row.value,
    )
    # Delete-then-create rather than update, so that two processes both finding
    # the lock stale still cannot both win: only one delete matches.
    deleted, _ = State.objects.using(using).filter(key=LOCK_KEY, updated=row.updated).delete()
    if not deleted:
        return False
    return _acquire(using, stale_after)


def lock_holder() -> tuple[dict, object] | None:
    """``(holder, since)`` if the lock is held, else ``None``. For diagnostics."""
    row = State.objects.using(sitepulse_settings.DATABASE_ALIAS).filter(key=LOCK_KEY).first()
    return (row.value, row.updated) if row else None
