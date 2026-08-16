"""The management commands -- the whole nightly story."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from sitepulse.identity import current_salt, reset_salt_cache
from sitepulse.models import DailyPageStat, DailyUniqueVisitor, Hit, Salt
from sitepulse.rollup import rolled_up_through

from .factories import make_hit

pytestmark = pytest.mark.django_db


def run(command, *args, **kwargs) -> str:
    out = StringIO()
    call_command(command, *args, stdout=out, stderr=out, **kwargs)
    return out.getvalue()


def at(day, hour=12):
    return timezone.make_aware(
        timezone.datetime(day.year, day.month, day.day, hour),
        timezone.get_current_timezone(),
    )


# ---------------------------------------------------------------------------
# rollup
# ---------------------------------------------------------------------------


def test_rollup_catches_up_on_every_missing_day():
    today = timezone.localdate()
    for offset in (3, 2, 1):
        make_hit(ts=at(today - timedelta(days=offset)))
    output = run("sitepulse_rollup")
    assert DailyPageStat.objects.values("date").distinct().count() == 3
    assert rolled_up_through() == today - timedelta(days=1)
    assert "hits ->" in output


def test_rollup_leaves_today_alone_unless_asked():
    today = timezone.localdate()
    make_hit(ts=at(today))
    run("sitepulse_rollup")
    assert not DailyPageStat.objects.filter(date=today).exists()
    run("sitepulse_rollup", "--today")
    assert DailyPageStat.objects.filter(date=today).exists()


def test_rollup_accepts_an_explicit_range():
    today = timezone.localdate()
    make_hit(ts=at(today - timedelta(days=10)))
    start = (today - timedelta(days=11)).isoformat()
    end = (today - timedelta(days=9)).isoformat()
    run("sitepulse_rollup", **{"date_from": start, "date_to": end})
    assert DailyPageStat.objects.filter(date=today - timedelta(days=10)).exists()


def test_rollup_rejects_a_nonsense_date():
    with pytest.raises(CommandError):
        run("sitepulse_rollup", date="not-a-date")


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


def test_a_brand_new_install_can_run_the_nightly_job():
    """Day one: hits collected today, nothing rolled up yet, nothing old enough
    to prune. That must not be an error -- it is every new project's first cron
    run, and failing it would email a traceback to everyone who installs this."""
    make_hit(ts=at(timezone.localdate()))
    output = run("sitepulse_nightly")
    assert "No raw hits are outside the retention window" in output


def test_prune_is_quiet_when_there_is_nothing_to_prune():
    run("sitepulse_prune")   # completely empty database
    make_hit(ts=at(timezone.localdate()))
    output = run("sitepulse_prune")
    assert "No raw hits are outside the retention window" in output


@override_settings(SITEPULSE={"SYNCHRONOUS_WRITES": True, "RAW_RETENTION_DAYS": 30})
def test_prune_refuses_to_discard_days_the_rollups_have_not_covered():
    make_hit(ts=at(timezone.localdate() - timedelta(days=60)))
    with pytest.raises(CommandError, match="Run sitepulse_rollup first"):
        run("sitepulse_prune")
    assert Hit.objects.count() == 1


@override_settings(SITEPULSE={"SYNCHRONOUS_WRITES": True, "RAW_RETENTION_DAYS": 30})
def test_prune_removes_raw_hits_outside_the_window_but_keeps_the_rollups():
    today = timezone.localdate()
    old, recent = today - timedelta(days=60), today - timedelta(days=2)
    make_hit(ts=at(old))
    make_hit(ts=at(recent))
    run("sitepulse_rollup", **{"date_from": old.isoformat(), "date_to": recent.isoformat()})

    run("sitepulse_prune")

    assert not Hit.objects.filter(ts__lt=at(today - timedelta(days=30))).exists()
    assert Hit.objects.filter(ts=at(recent)).exists()
    assert DailyPageStat.objects.filter(date=old).exists()   # history survives


@override_settings(SITEPULSE={"SYNCHRONOUS_WRITES": True, "RAW_RETENTION_DAYS": 30})
def test_prune_dry_run_changes_nothing():
    make_hit(ts=at(timezone.localdate() - timedelta(days=60)))
    run("sitepulse_rollup")
    run("sitepulse_prune", dry_run=True)
    assert Hit.objects.count() == 1


@override_settings(SITEPULSE={"SYNCHRONOUS_WRITES": True, "UNIQUE_VISITOR_RETENTION_DAYS": 30})
def test_prune_trims_the_visitor_table_that_is_not_negligible():
    today = timezone.localdate()
    DailyUniqueVisitor.objects.create(date=today - timedelta(days=400), visitor_hash=b"a" * 16)
    DailyUniqueVisitor.objects.create(date=today, visitor_hash=b"b" * 16)
    make_hit(ts=at(today - timedelta(days=1)))
    run("sitepulse_rollup")
    run("sitepulse_prune", force=True)
    assert DailyUniqueVisitor.objects.filter(date=today - timedelta(days=400)).count() == 0


# ---------------------------------------------------------------------------
# salt
# ---------------------------------------------------------------------------


def test_rotate_salt_destroys_every_older_salt():
    yesterday = timezone.localdate() - timedelta(days=1)
    Salt.objects.create(date=yesterday, value=b"yesterday")
    reset_salt_cache()
    output = run("sitepulse_rotate_salt")
    assert not Salt.objects.filter(date=yesterday).exists()
    assert Salt.objects.filter(date=timezone.localdate()).exists()
    assert "can no longer be recomputed" in output


def test_rotate_salt_does_not_change_todays_salt():
    reset_salt_cache()
    before = current_salt()
    run("sitepulse_rotate_salt")
    reset_salt_cache()
    assert current_salt() == before


# ---------------------------------------------------------------------------
# partitions / nightly
# ---------------------------------------------------------------------------


def test_partitions_command_is_safe_on_any_backend():
    output = run("sitepulse_partitions")
    assert "Ensured" in output or "Not running on PostgreSQL" in output


def test_partitions_check_reports_the_catch_all():
    output = run("sitepulse_partitions", check=True)
    assert "empty, as it should be" in output or "Not running on PostgreSQL" in output


def test_nightly_runs_the_whole_sequence():
    make_hit(ts=at(timezone.localdate() - timedelta(days=1)))
    output = run("sitepulse_nightly")
    assert "sitepulse_rotate_salt" in output
    assert "sitepulse_rollup" in output
    assert DailyPageStat.objects.exists()


def test_nightly_takes_a_lock_and_a_second_run_stands_down():
    """Beat schedulers double-fire on rolling deploys and cron ends up on two
    boxes; concurrent rollups of the same day can collide on a grain."""
    from sitepulse.scheduling import lock_holder, nightly_lock

    make_hit(ts=at(timezone.localdate() - timedelta(days=1)))
    with nightly_lock():
        assert lock_holder() is not None
        output = run("sitepulse_nightly")
    assert "Nothing to do" in output
    assert not DailyPageStat.objects.exists()   # deferred, not silently skipped

    assert lock_holder() is None                # released on the way out
    run("sitepulse_nightly")
    assert DailyPageStat.objects.exists()


def test_the_lock_is_released_even_when_a_step_fails(monkeypatch):
    from sitepulse.scheduling import lock_holder

    def explode(*args, **kwargs):
        raise CommandError("prune exploded")

    monkeypatch.setattr(
        "sitepulse.management.commands.sitepulse_prune.Command.handle", explode
    )
    with pytest.raises(CommandError):
        run("sitepulse_nightly")
    assert lock_holder() is None


def test_a_lock_left_behind_by_a_killed_process_is_taken_over():
    from datetime import timedelta as td

    from sitepulse.models import State
    from sitepulse.scheduling import LOCK_KEY, nightly_lock

    State.objects.create(key=LOCK_KEY, value={"host": "gone", "pid": 1})
    State.objects.filter(key=LOCK_KEY).update(updated=timezone.now() - td(days=1))

    with nightly_lock():
        pass  # acquired: a day-old claim is debris, not a running job
    assert not State.objects.filter(key=LOCK_KEY).exists()


def test_the_lock_can_be_skipped_deliberately():
    from sitepulse.scheduling import nightly_lock

    make_hit(ts=at(timezone.localdate() - timedelta(days=1)))
    with nightly_lock():
        run("sitepulse_nightly", no_lock=True)
    assert DailyPageStat.objects.exists()


def test_nightly_stops_if_a_step_fails(monkeypatch):
    def explode(*args, **kwargs):
        raise CommandError("rollup exploded")

    monkeypatch.setattr(
        "sitepulse.management.commands.sitepulse_rollup.Command.handle", explode
    )
    with pytest.raises(CommandError, match="sitepulse_rollup failed"):
        run("sitepulse_nightly")
