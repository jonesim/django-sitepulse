# django-sitepulse

Self-hosted web analytics and request performance data, collected **inside** your
Django project and stored in your own database. No third-party service, no
cookie, no JavaScript.

It replaces the reporting layer of Google Analytics for the two things a Django
project actually needs:

- **Traffic** — pageviews, visitors, sessions, referrers, entry and exit pages,
  device and country breakdowns.
- **Performance and errors** — response time distributions per view, 4xx/5xx
  rates, slow endpoints, and SQL query counts per request.

Designed for **100k–5M pageviews/month**, which at 5M/month is about **1.9
writes per second** — no queue, no Redis, no separate analytics database.

## Why collect from inside the framework

Three things fall out of owning the data that a separate analytics service
structurally cannot give you:

1. **You can join analytics to your own models.**
   `Hit.objects.filter(user__subscription__plan="pro")` is a query GA can never
   answer, because GA has no idea what a subscription is.
2. **You record the URL *route*, not just the path.** Django knows
   `/orders/8814/` matched `orders/<int:pk>/`. A hosted tool sees 50,000
   distinct URLs; you see one endpoint with 50,000 hits. That single fact kills
   the biggest source of noise in web analytics, and it is only possible from
   inside the framework.
3. **Traffic and performance share a row.** "Which pages are both popular and
   slow" is one query, not a correlation exercise across two tools.

---

## Install

```bash
pip install django-sitepulse                 # collector only, two dependencies
pip install "django-sitepulse[dashboard]"    # + the built-in dashboard
pip install "django-sitepulse[geoip]"        # + country lookup
```

```python
INSTALLED_APPS = [
    ...,
    "sitepulse",
]

MIDDLEWARE = [
    ...,
    # Last, or as late as you can: it times everything below it.
    "sitepulse.middleware.AnalyticsMiddleware",
]
```

```bash
python manage.py migrate
```

That is the whole setup. Data starts landing immediately.

For the dashboard, add the URLs and install the extra:

```python
path("analytics/", include("sitepulse.urls", namespace="sitepulse")),
```

Then one cron entry, nightly:

```bash
python manage.py sitepulse_nightly
```

`sitepulse_nightly` rotates the visitor salt, creates next month's partitions,
rolls up every outstanding day and prunes raw hits past the retention window —
in that order, and it stops if a step fails so pruning can never run after a
failed rollup.

---

## How it works

```
                 ┌───────────────────────────────────────┐
   HTTP request  │  AnalyticsMiddleware                  │
  ──────────────►│  • times the request                  │
                 │  • reads resolver_match.route         │
                 │  • counts SQL via execute_wrapper()   │
                 └──────────────┬────────────────────────┘
                                │  a plain dataclass — no ORM, no I/O
                                ▼
                 ┌───────────────────────────────────────┐
                 │  bounded deque + background thread    │
                 │  flush on 100 rows or 5s              │
                 │  enrich → hash → sessions → insert    │
                 └──────────────┬────────────────────────┘
                                │  bulk_create(batch)
                                ▼
                 ┌───────────────────────────────────────┐
                 │  sitepulse_hit  (raw, partitioned by  │
                 │  month, 90-day retention)             │
                 └──────────────┬────────────────────────┘
                                │  manage.py sitepulse_rollup
                                ▼
                 ┌───────────────────────────────────────┐
                 │  sitepulse_daily_*  (kept forever)    │
                 └──────────────┬────────────────────────┘
                                ▼
                 ┌───────────────────────────────────────┐
                 │  Report API · dashboard · admin       │
                 └───────────────────────────────────────┘
```

**The request path does no I/O at all.** It builds a dataclass and appends it to
a deque, which is nanoseconds. Hashing, user-agent parsing, geo lookup, session
assignment and the insert all happen in a background thread, a whole batch at a
time. Analytics never adds a synchronous write to a request, because that write
lands squarely in your p99.

**The buffer is bounded** (10,000 by default). On overflow it drops the *oldest*
rows and counts them, and the count is shown on the dashboard's Health page.
Analytics must never be able to take the site down, and a bounded buffer with
visible loss beats an unbounded one with an OOM.

**Each worker process has its own buffer and thread.** With 8 Gunicorn workers
that is up to 8 batched inserts every 5 seconds. At the volumes above that is
fine; it is also exactly the part that stops being fine if you get 50× busier,
at which point `buffer.py` is the only file that has to change.

---

## Privacy: cookieless by default

```
visitor_hash = sha256(daily_salt + host + ip + user_agent)[:16]
```

- The salt **rotates every 24 hours and the old one is destroyed** — creating a
  new day's salt deletes every earlier one, so it happens whether or not the
  cron ran.
- **Raw IPs and user-agent strings are never written anywhere.** They exist in
  memory for at most five seconds and are cleared as soon as enrichment is done.
  What is stored is the 16-byte hash plus coarse derived values: browser family,
  OS family, device class, ISO country code.
- Because the salt is gone, there is no mechanism — even with full database
  access — to link a visitor across days.

This is the same construction [Plausible](https://plausible.io/data-policy)
uses. No cookie means no cookie banner under the ePrivacy Directive, and no PII
at rest means nothing to hand over in a subject access request.

**What you give up:** returning-visitor metrics. Every day, everyone is new.
`Report.visitors()` counts **visitor-days**, and the dashboard labels it as
such — a person visiting on three days counts three times and there is no way to
know otherwise. That is the honest cost of not setting a cookie.

If you need multi-day identity, `RETURNING_VISITORS = True` switches to a
first-party cookie with a random id. **In the UK/EU that needs consent** — point
`CONSENT_CHECK` at a `callable(request) -> bool` and requests without consent
transparently fall back to the cookieless hash, so traffic counts stay complete
either way.

Logged-in users are separate and deliberately opt-in:
`TRACK_AUTHENTICATED_USER_ID = True` stores a nullable FK to your user model.
That is a different privacy posture — identified, first-party, covered by your
account terms — so it is a different field and a different switch.

---

## Reading the data

### The `Report` API

```python
from datetime import date
from sitepulse.query import Report

start, end = date(2026, 8, 1), date(2026, 8, 16)

Report.summary(start, end)                                  # headline numbers
Report.timeseries(start, end)                               # per-day views/sessions/visitors
Report.pageviews(start, end, group_by="route", limit=20)
Report.visitors(start, end, granularity="day")
Report.sources(start, end, group_by="referrer_host")
Report.audience(start, end, dimension="country")
Report.performance(start, end, route="/orders/<int:pk>")    # p50/p95/p99 + histogram
Report.performance_trend(start, end)                        # p95 over time
Report.errors(start, end, min_status=400)
Report.error_rate(start, end)
Report.health()                                             # drops, rollup lag
```

Every method takes inclusive dates and excludes bot traffic unless
`include_bots=True`. Rolled-up days are read from the small daily tables; days
the nightly job has not covered yet are aggregated from raw hits **with the same
aggregator the rollup uses**, so today's numbers and tomorrow's rollup of today
cannot disagree. Live aggregation is cached for `LIVE_CACHE_SECONDS`.

Two honesty rules are enforced in code rather than left to the caller:

- **Distinct counts are never summed across rows.** `Report.pageviews()` only
  returns `visitors` for a single-day range; range-wide uniques come from
  `Report.visitors()`, which reads a `(date, visitor)` table and is exact. Where
  a grouping makes `sessions` an upper bound rather than exact, the row says so
  in `sessions_exact`.
- **Percentiles come from summable histogram buckets**, so they are accurate to
  within a bucket width and stay available forever — long after the raw rows
  they came from were dropped. Averaging or storing a mean would hide exactly
  the tail you care about.

### The dashboard

Six pages — Overview, Pages, Sources, Performance, Errors, Health — behind the
`sitepulse.view_dashboard` permission (superusers always pass). Server-rendered,
with inline SVG charts and no JavaScript at all. Charts are theme-aware and every
one of them has a "Show the numbers" table underneath.

The Health page is the one to look at when something seems off: it shows dropped
hits, failed write batches, how far behind the rollups are, the partition list,
and every setting in force.

---

## Storage and scale

Roughly **350 bytes per hit** all-in (≈225 bytes heap + three indexes).

| Volume | Rows/year | Raw, unbounded | Raw @ 90-day retention |
|---|---|---|---|
| 1M views/mo | 12M | ~4.2 GB/yr | ~1.05 GB steady |
| 5M views/mo | 60M | ~21 GB/yr | ~5.3 GB steady |

Rollups add 1–5 MB/month regardless of traffic. The one table that is not
negligible is `sitepulse_daily_visitor`, at roughly 1.1–4.3 GB/year for a busy
site — `UNIQUE_VISITOR_RETENTION_DAYS` (default 2 years) trims it, at the cost of
exact range-wide uniques for older periods.

### Partitioning (PostgreSQL)

`sitepulse_hit` is `PARTITION BY RANGE (ts)`, one partition per month, so
retention is a `DROP TABLE` rather than a lock-holding `DELETE` over tens of
millions of rows. There is no third-party dependency for this: the `CREATE TABLE`
is generated from the model's own column definitions and about fifteen lines of
SQL, so it cannot drift out of step with the ORM.

A catch-all `DEFAULT` partition is the safety net — a hit with an unexpected
timestamp is kept rather than rejected. It should always be empty;
`sitepulse_partitions --check` tells you when it isn't.

**On other backends** everything works except partitioning (retention falls back
to chunked deletes) and exact percentiles. The package is PostgreSQL-first with a
degraded fallback, rather than pretending at database neutrality.

---

## Settings

All in one namespaced dict, validated at startup — an unknown key or a bad value
is an `ImproperlyConfigured` at boot, not a silent default.

```python
SITEPULSE = {
    "ENABLED": True,
    # identity
    "RETURNING_VISITORS": False,     # True = first-party cookie = needs consent
    "CONSENT_CHECK": None,           # "myapp.privacy.has_consented"
    "TRACK_AUTHENTICATED_USER_ID": False,
    "SESSION_TIMEOUT_MINUTES": 30,
    # collection
    "TRACK_QUERY_COUNTS": True,
    "TRACK_REFERRER_PATH": False,
    "TRACK_REGION": False,
    "GEOIP_ENABLED": False,
    "EXCLUDE_PATHS": [r"^/static/", r"^/media/", r"^/admin/", r"^/healthz"],
    "EXCLUDE_METHODS": ["HEAD", "OPTIONS"],
    "EXCLUDE_STATUS": [],
    "EXCLUDE_STAFF": True,
    "EXTRA_BOT_UA_PATTERNS": [],
    # retention
    "RAW_RETENTION_DAYS": 90,
    "UNIQUE_VISITOR_RETENTION_DAYS": 730,
    "PARTITION_MONTHS_AHEAD": 2,
    # buffer
    "BUFFER_MAX": 10_000,
    "FLUSH_EVERY_ROWS": 100,
    "FLUSH_EVERY_SECONDS": 5,
    # reporting
    "DURATION_BUCKETS_MS": [25, 50, 100, 250, 500, 1000, 2500, 5000],
    "LIVE_CACHE_SECONDS": 60,
    "DASHBOARD_PERMISSION": "sitepulse.view_dashboard",
    "DASHBOARD_DEFAULT_DAYS": 30,
    # plumbing
    "DATABASE_ALIAS": "default",
    "CACHE_ALIAS": "default",
}
```

`DATABASE_ALIAS` will be `"default"` for years, and it is there from day one
because it is the seam that lets you move analytics to a replica or its own
database later without touching a single call site.

`DURATION_BUCKETS_MS` must be exactly eight boundaries (the schema has nine
bucket columns) and, once rollups exist, changing it is refused —
`--allow-bucket-change` overrides, but the columns are positional, so old rows
will mean something different from new ones.

**Sessions need a shared cache.** With a per-process `LocMemCache`, every worker
sees its own sessions and session, entry, exit and bounce counts come out
inflated. `manage.py check` warns about this (`sitepulse.W002`).

### Bots

Bots are 30–50% of raw hits. They are **flagged, never dropped** — `is_bot` on
the hit *and* as part of the grain in every rollup table, so the filter stays
auditable long after the raw rows are gone. Reports exclude them by default;
the dashboard has an "Include bots" toggle. Discarded data is gone forever; a
wrong boolean is a backfill.

---

## Commands

| Command | What it does |
|---|---|
| `sitepulse_nightly` | All of the below, in the right order. One cron entry. |
| `sitepulse_rollup` | Aggregates every day with hits but no rollup. Idempotent. |
| `sitepulse_prune` | Drops partitions / rows past retention. Refuses to discard days the rollups have not covered. |
| `sitepulse_partitions` | Creates upcoming monthly partitions. `--check` audits them. |
| `sitepulse_rotate_salt` | Ensures today's salt exists and destroys every older one. |

## Scheduling

`sitepulse_nightly` is an ordinary management command, so cron, systemd timers,
Kubernetes CronJobs, Heroku Scheduler and Celery Beat all drive it the same way.
It takes a **database lock** for the duration of the run, so starting a second
copy while one is going is safe: the second exits quietly, and the work is
deferred to the run that holds the lock rather than skipped. A lock left behind
by a killed process is taken over after six hours.

That matters more than it sounds: rolling deploys double-fire beat schedulers,
cron entries end up on two boxes, and people run things by hand — and rolling up
a day deletes and rewrites it inside a transaction, so two at once can deadlock
or collide on a grain's unique constraint.

### Celery Beat

Two tasks ship ready to schedule. **Celery is not a dependency** — nothing in
the package imports it, and `sitepulse/tasks.py` degrades to plain functions if
it isn't installed, so the module is inert on projects that have never heard of
Celery. If your app calls `autodiscover_tasks()`, they register themselves; there
is nothing to add to `INSTALLED_APPS` beyond `sitepulse`.

| Task | Suggested schedule |
|---|---|
| `sitepulse.nightly` | once a day |
| `sitepulse.rollup_today` | every 15 minutes, optional |

```python
from celery.schedules import crontab

app.conf.beat_schedule = {
    "sitepulse-nightly": {
        "task": "sitepulse.nightly",
        "schedule": crontab(hour=3, minute=15),
    },
    # Optional. Days the nightly job has not covered yet are aggregated from raw
    # hits on demand, which is what keeps the dashboard current and is also the
    # most expensive thing it does on a busy site. This trades a little
    # freshness for much cheaper page loads.
    "sitepulse-rollup-today": {
        "task": "sitepulse.rollup_today",
        "schedule": crontab(minute="*/15"),
    },
}
```

With `django-celery-beat`, point a `PeriodicTask` at the task name instead.

**Keep them off your latency-sensitive queues.** A normal night is seconds, but a
backlog rolls up each missing day in turn. The tasks already carry
`time_limit=3600` / `soft_time_limit=3300`, because a soft timeout part-way
through leaves a day half-written — idempotent, so the next run repairs it, but
you would rather it finished.

**No lock of your own is needed**; `sitepulse_nightly` already holds one. There is
no autoretry either, deliberately: a retry storm against a struggling database is
worse than one missed night, and the next scheduled run picks the day up anyway.

Without Celery installed, the same functions are ordinary callables:

```python
from sitepulse.tasks import nightly
nightly()
```

---

## Non-goals

Heatmaps, session replay, funnels, cohort analysis, A/B testing, cross-device
identity, real-time streaming, custom business events. The `props` JSON column on
`Hit` is an unused escape hatch so that attaching one project-specific field
later does not mean a migration on a 20-million-row table.

## Requirements

Django 5.2+, Python 3.10+. PostgreSQL recommended. Two runtime dependencies:
Django and `user-agents`.

## Licence

MIT.
