"""PostgreSQL range partitioning for the raw hit table.

At 60M rows a year, ``DELETE FROM sitepulse_hit WHERE ts < ...`` is a
long-running, lock-holding, vacuum-generating operation you would rather not run
every month. Monthly range partitions turn retention into ``DROP TABLE``, which
is instant.

Django has no declarative partitioning, and the obvious third-party option
(``django-postgres-extra``) currently pins ``Django<6.0``, which would cap this
package at Django 5.x. So: raw SQL, about fifteen lines of it, generated from the
model's own column definitions so it cannot drift out of step with the ORM.

Everything here is a no-op on non-PostgreSQL backends, where the table is created
as an ordinary one and retention falls back to chunked deletes.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from django.db import connections

from .conf import sitepulse_settings

PARENT_TABLE = "sitepulse_hit"
DEFAULT_PARTITION = f"{PARENT_TABLE}_default"
_PARTITION_RE = re.compile(rf"^{PARENT_TABLE}_(\d{{4}})_(\d{{2}})$")


def db():
    return connections[sitepulse_settings.DATABASE_ALIAS]


def is_postgres(connection=None) -> bool:
    return (connection or db()).vendor == "postgresql"


# ---------------------------------------------------------------------------
# month arithmetic
# ---------------------------------------------------------------------------


def month_start(value: date) -> date:
    return value.replace(day=1)


def next_month(value: date) -> date:
    return (value.replace(day=1) + timedelta(days=32)).replace(day=1)


def partition_name(value: date) -> str:
    return f"{PARENT_TABLE}_{value.year:04d}_{value.month:02d}"


def _bound(value: date) -> str:
    return datetime(value.year, value.month, 1, tzinfo=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------


def create_hit_table(schema_editor, model) -> None:
    """Create ``sitepulse_hit`` as a partitioned table, plus its indexes.

    The column list comes from ``schema_editor.table_sql(model)`` -- the same
    call ``create_model()`` makes -- so column types, nullability and the
    ``user_id`` reference to whatever ``AUTH_USER_MODEL`` uses are all whatever
    Django would have produced. Two edits are then applied:

    1. the inline ``PRIMARY KEY`` on ``id`` is removed and replaced by a
       composite ``(id, ts)``, because PostgreSQL requires the partition key to
       be part of every unique constraint;
    2. ``PARTITION BY RANGE (ts)`` is appended.

    The ORM still treats ``id`` alone as the primary key, which is correct in
    practice: every partition draws from one identity sequence on the parent.
    """
    sql, params = schema_editor.table_sql(model)
    if params:  # pragma: no cover - Hit has no parameterised defaults
        raise RuntimeError("sitepulse: unexpected parameterised DDL for the hit table")

    if " PRIMARY KEY" not in sql:  # pragma: no cover - guards a Django change
        raise RuntimeError("sitepulse: could not find the inline primary key to relocate")
    sql = sql.replace(" PRIMARY KEY", "", 1)

    closing = sql.rfind(")")
    quote = schema_editor.quote_name
    pk_columns = ", ".join(quote(name) for name in ("id", "ts"))
    sql = f"{sql[:closing]}, PRIMARY KEY ({pk_columns}){sql[closing:]}"
    sql += f" PARTITION BY RANGE ({quote('ts')})"

    schema_editor.execute(sql)

    # Indexes on the parent propagate to every partition, existing and future.
    for index in model._meta.indexes:
        schema_editor.add_index(model, index)

    create_default_partition(schema_editor)
    today = date.today()
    ensure_partitions(
        months_ahead=sitepulse_settings.PARTITION_MONTHS_AHEAD,
        from_date=today,
        schema_editor=schema_editor,
    )


def _execute(sql: str, schema_editor=None) -> None:
    if schema_editor is not None:
        schema_editor.execute(sql)
        return
    with db().cursor() as cursor:
        cursor.execute(sql)


def create_default_partition(schema_editor=None) -> None:
    """A catch-all partition so a hit with an unexpected timestamp is never lost.

    It should stay empty; ``sitepulse_partitions --check`` complains if it
    doesn't, because rows in here mean a month partition was missing.
    """
    _execute(
        f'CREATE TABLE IF NOT EXISTS "{DEFAULT_PARTITION}" '
        f'PARTITION OF "{PARENT_TABLE}" DEFAULT',
        schema_editor,
    )


def create_partition(month: date, schema_editor=None) -> str:
    start = month_start(month)
    end = next_month(start)
    name = partition_name(start)
    _execute(
        f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF "{PARENT_TABLE}" '
        f"FOR VALUES FROM ('{_bound(start)}') TO ('{_bound(end)}')",
        schema_editor,
    )
    return name


def ensure_partitions(months_ahead: int | None = None, from_date: date | None = None,
                      schema_editor=None) -> list[str]:
    """Create this month's partition and the next ``months_ahead``.

    Idempotent, so running it nightly is the whole maintenance story.
    """
    if not is_postgres():
        return []
    if months_ahead is None:
        months_ahead = sitepulse_settings.PARTITION_MONTHS_AHEAD
    current = month_start(from_date or date.today())
    created = []
    for _ in range(months_ahead + 1):
        created.append(create_partition(current, schema_editor))
        current = next_month(current)
    return created


def existing_partitions() -> list[tuple[str, date | None]]:
    """``[(table_name, month_or_None), ...]`` for every child of the hit table."""
    if not is_postgres():
        return []
    with db().cursor() as cursor:
        cursor.execute(
            """
            SELECT child.relname
            FROM pg_inherits
            JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
            JOIN pg_class child ON pg_inherits.inhrelid = child.oid
            WHERE parent.relname = %s
            ORDER BY child.relname
            """,
            [PARENT_TABLE],
        )
        names = [row[0] for row in cursor.fetchall()]
    result = []
    for name in names:
        match = _PARTITION_RE.match(name)
        result.append((name, date(int(match[1]), int(match[2]), 1) if match else None))
    return result


def drop_partitions_before(cutoff: date) -> list[str]:
    """Drop every partition that ends on or before ``cutoff``. Returns their names.

    A partition is only dropped when *all* of its rows are older than the
    retention window -- the month containing the cutoff is left alone rather than
    losing data that is still inside it.
    """
    dropped = []
    for name, month in existing_partitions():
        if month is None:
            continue
        if next_month(month) <= cutoff:
            _execute(f'DROP TABLE IF EXISTS "{name}"')
            dropped.append(name)
    return dropped


def prune_default_partition(before) -> int:
    """Delete old rows out of the catch-all partition.

    Dropping partitions never touches this one, so without this any hit that
    arrived while its month was missing would sit here past retention forever.
    It should always be empty, so this is normally a no-op that costs one
    statement a night.
    """
    if not is_postgres():
        return 0
    with db().cursor() as cursor:
        cursor.execute(f'DELETE FROM "{DEFAULT_PARTITION}" WHERE "ts" < %s', [before])
        return cursor.rowcount or 0


def default_partition_rows() -> int:
    """How many rows landed in the catch-all. Should always be zero."""
    if not is_postgres():
        return 0
    with db().cursor() as cursor:
        cursor.execute(f'SELECT count(*) FROM "{DEFAULT_PARTITION}"')
        return cursor.fetchone()[0]
