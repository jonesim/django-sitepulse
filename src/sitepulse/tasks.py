"""Celery tasks -- optional, and importable whether or not Celery is installed.

Celery's ``autodiscover_tasks()`` imports ``<app>.tasks`` for every entry in
``INSTALLED_APPS``, so this module exists to save every project writing the same
three-line wrapper. It is **not** a dependency: ``celery`` is not in this
package's requirements, nothing else in ``sitepulse`` imports this module, and if
Celery is absent the decorator below degrades to a no-op so the functions stay
ordinary callables. Importing this file on a project with no Celery does nothing
and costs nothing.

With Celery installed::

    app.conf.beat_schedule = {
        "sitepulse-nightly": {
            "task": "sitepulse.nightly",
            "schedule": crontab(hour=3, minute=15),
        },
    }

or point a ``django-celery-beat`` ``PeriodicTask`` at ``sitepulse.nightly``.

Without it, the same functions still work -- they are just plain functions::

    from sitepulse.tasks import nightly
    nightly()

Neither task takes a lock of its own: ``sitepulse_nightly`` already holds one for
the duration of its run (see :mod:`sitepulse.scheduling`), so a double-fired
schedule is safe.
"""

from __future__ import annotations

import logging
from io import StringIO

from django.core.management import call_command

logger = logging.getLogger("sitepulse")

try:  # pragma: no cover - depends on what is installed
    from celery import shared_task
except ImportError:  # pragma: no cover - the whole point of this module
    def shared_task(*args, **kwargs):
        """Stand-in for Celery's decorator.

        Returns the function untouched, so everything here stays importable and
        directly callable. Only ``.delay()``/``.apply_async()`` need Celery, and
        those are exactly the calls a project without Celery will not be making.
        """
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def decorate(function):
            return function

        return decorate


def _run(command: str, **options) -> str:
    """Run a management command, returning its output and logging it.

    A worker's stdout usually goes somewhere far less useful than its logger, and
    a nightly job that says nothing about what it did is a nightly job nobody
    notices has stopped.
    """
    out = StringIO()
    try:
        call_command(command, stdout=out, stderr=out, **options)
    except Exception:
        logger.exception("sitepulse: %s failed\n%s", command, out.getvalue())
        raise
    output = out.getvalue()
    logger.info("sitepulse: %s\n%s", command, output)
    return output


@shared_task(
    name="sitepulse.nightly",
    # Generous, and deliberately so: a normal night is seconds, but a backlog
    # rolls up each missing day in turn, and a soft timeout part-way through
    # leaves a day half-written. It is idempotent, so the next run repairs it --
    # better not to need repairing.
    time_limit=3600,
    soft_time_limit=3300,
    # No autoretry: the command is guarded by its own lock and a retry storm on a
    # database problem is worse than one missed night.
    ignore_result=True,
)
def nightly(skip_prune: bool = False) -> str:
    """Rotate the salt, create partitions, roll up outstanding days, prune.

    Schedule this once a day. It is safe to fire more than once -- the second
    run finds the lock held and stands down.
    """
    return _run("sitepulse_nightly", skip_prune=skip_prune)


@shared_task(
    name="sitepulse.rollup_today",
    time_limit=900,
    soft_time_limit=840,
    ignore_result=True,
)
def rollup_today() -> str:
    """Roll up today so far, instead of leaving it to be aggregated live.

    Optional. Days the nightly job has not covered are aggregated from raw hits
    on demand (cached for ``LIVE_CACHE_SECONDS``), which is what keeps the
    dashboard current. On a busy site that scan is the most expensive thing the
    dashboard does; scheduling this every 15 minutes or so trades a little
    freshness for much cheaper page loads.
    """
    return _run("sitepulse_rollup", today=True)
