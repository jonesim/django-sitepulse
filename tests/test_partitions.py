"""PostgreSQL partitioning. Skipped entirely on other backends."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.db import connection
from django.utils import timezone

from sitepulse import partitions
from sitepulse.models import Hit
from sitepulse.query import exact_percentiles

from .factories import make_hit

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="partitioning, jsonb and percentile_cont are the PostgreSQL-only half",
    ),
]


def test_the_hit_table_is_actually_partitioned():
    with connection.cursor() as cursor:
        cursor.execute("SELECT relkind FROM pg_class WHERE relname = %s", ["sitepulse_hit"])
        assert cursor.fetchone()[0] == "p"


def test_the_model_and_the_hand_written_ddl_agree_on_columns():
    """The CREATE TABLE is generated from the model, so this should never drift --
    but the whole design rests on it, so check rather than assume."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            ["sitepulse_hit"],
        )
        columns = {row[0] for row in cursor.fetchall()}
    assert columns == {field.column for field in Hit._meta.local_fields}


def test_the_primary_key_includes_the_partition_key():
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'sitepulse_hit'::regclass AND i.indisprimary
            """
        )
        assert {row[0] for row in cursor.fetchall()} == {"id", "ts"}


def test_this_month_has_a_partition_and_rows_land_in_it():
    make_hit()
    name = partitions.partition_name(timezone.localdate())
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT count(*) FROM "{name}"')
        assert cursor.fetchone()[0] == 1


def test_the_catch_all_partition_exists_and_stays_empty():
    make_hit()
    names = {name for name, _ in partitions.existing_partitions()}
    assert partitions.DEFAULT_PARTITION in names
    assert partitions.default_partition_rows() == 0


def test_creating_partitions_is_idempotent():
    first = partitions.ensure_partitions(months_ahead=3)
    second = partitions.ensure_partitions(months_ahead=3)
    assert first == second
    assert len(first) == 4


def test_retention_drops_whole_months_and_leaves_partial_ones():
    old = date(2025, 1, 15)
    partitions.create_partition(old)
    partitions.create_partition(date(2025, 2, 15))
    assert partitions.partition_name(old) in {n for n, _ in partitions.existing_partitions()}

    # A cutoff inside February drops January only: February still holds rows
    # that are inside the retention window.
    dropped = partitions.drop_partitions_before(date(2025, 2, 20))
    assert partitions.partition_name(old) in dropped
    assert partitions.partition_name(date(2025, 2, 15)) not in dropped


def test_exact_percentiles_come_from_the_raw_rows():
    today = timezone.localdate()
    for duration in range(1, 101):
        make_hit(duration_ms=duration)
    result = exact_percentiles(today, today)
    assert result["exact"] is True
    assert 49 <= result["p50"] <= 51
    assert 94 <= result["p95"] <= 96


def test_a_hit_far_in_the_future_is_kept_not_lost():
    """The catch-all partition is the safety net for a missing month."""
    future = timezone.now() + timedelta(days=3650)
    make_hit(ts=future)
    assert Hit.objects.filter(ts=future).exists()
    assert partitions.default_partition_rows() == 1
