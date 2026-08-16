"""The optional Celery tasks -- which must work, and must not require Celery."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from contextlib import contextmanager
from datetime import timedelta

import pytest
from django.core.management.base import CommandError
from django.utils import timezone

from sitepulse import tasks
from sitepulse.models import DailyPageStat

from .factories import make_hit

pytestmark = pytest.mark.django_db


def at(day, hour=12):
    return timezone.make_aware(
        timezone.datetime(day.year, day.month, day.day, hour),
        timezone.get_current_timezone(),
    )


@contextmanager
def celery_missing():
    """Make ``import celery`` fail, whatever is actually installed.

    Putting ``None`` in ``sys.modules`` is the documented way to do this, and it
    means the fallback path is tested on every machine rather than only on ones
    that happen not to have Celery.
    """
    original = sys.modules.get("celery", _MISSING)
    sys.modules["celery"] = None
    try:
        yield importlib.reload(tasks)
    finally:
        if original is _MISSING:
            sys.modules.pop("celery", None)
        else:
            sys.modules["celery"] = original
        importlib.reload(tasks)


_MISSING = object()


def test_the_module_works_without_celery_installed():
    """`celery` is not a dependency of this package, so importing the task module
    on a project that has never heard of it must work and cost nothing."""
    with celery_missing() as module:
        assert callable(module.nightly)
        assert callable(module.rollup_today)

        # The stand-in decorator handles both spellings Celery allows.
        def target():
            return "ran"

        assert module.shared_task(target) is target
        assert module.shared_task(name="x", time_limit=1)(target) is target


def test_the_tasks_still_run_without_celery():
    make_hit(ts=at(timezone.localdate() - timedelta(days=1)))
    with celery_missing() as module:
        module.nightly()
    assert DailyPageStat.objects.exists()


@pytest.mark.skipif(
    importlib.util.find_spec("celery") is None, reason="Celery is not installed"
)
def test_with_celery_they_are_real_tasks():
    module = importlib.reload(tasks)
    assert module.nightly.name == "sitepulse.nightly"
    assert module.rollup_today.name == "sitepulse.rollup_today"
    assert hasattr(module.nightly, "delay")
    assert module.nightly.time_limit == 3600


def test_the_nightly_task_is_callable_directly():
    make_hit(ts=at(timezone.localdate() - timedelta(days=1)))
    output = tasks.nightly()
    assert DailyPageStat.objects.exists()
    assert "sitepulse_rollup" in output


def test_the_nightly_task_can_skip_pruning():
    make_hit(ts=at(timezone.localdate() - timedelta(days=1)))
    output = tasks.nightly(skip_prune=True)
    assert "sitepulse_prune" not in output


def test_rollup_today_covers_today():
    today = timezone.localdate()
    make_hit(ts=at(today))
    tasks.rollup_today()
    assert DailyPageStat.objects.filter(date=today).exists()


def test_a_failure_is_logged_and_re_raised(monkeypatch, caplog):
    def explode(*args, **kwargs):
        raise CommandError("rollup exploded")

    monkeypatch.setattr(
        "sitepulse.management.commands.sitepulse_rollup.Command.handle", explode
    )
    with caplog.at_level("ERROR", logger="sitepulse"), pytest.raises(CommandError):
        tasks.nightly()
    assert "failed" in caplog.text
