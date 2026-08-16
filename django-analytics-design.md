# Self-hosted analytics as a reusable Django app

**Design document — v1 for review**
Prepared for Ian, 16 August 2026

---

## 1. What this is

A pip-installable Django app that collects web analytics and request performance data
directly into your own database, with no third-party analytics service involved. It
replaces the reporting layer of Google Analytics for the two things you said you need:

- **Pageviews and traffic** — paths, unique visitors, sessions, referrers, entry and exit
  pages, device and country breakdowns.
- **Performance and errors** — response time distributions per view, 4xx/5xx rates, slow
  endpoints, exception counts.

Designed for **100k–5M pageviews/month**.

### Why this is a genuinely better shape than GA for a Django project

Three things fall out of owning the data that you simply cannot get from GA:

1. **You can join analytics to your own models.** `Hit.objects.filter(user__subscription__plan="pro")`
   is a query GA can never answer, because GA has no idea what a subscription is.
2. **You can record the URL *route*, not just the path.** Django knows that
   `/orders/8814/` matched the pattern `orders/<int:pk>/`. GA sees 50,000 distinct URLs;
   you see one endpoint with 50,000 hits. This kills the single biggest source of noise in
   web analytics for free, and it's only possible because the collector lives inside the
   framework.
3. **Traffic data and performance data share a row.** "Which pages are both popular and
   slow" is one query, not a correlation exercise across two tools.

### Non-goals for v1

Explicitly out of scope, to keep the surface small: heatmaps and session replay, funnel and
cohort analysis, A/B testing, cross-device identity, real-time streaming dashboards,
custom business events. The data model leaves room for custom events later (§4.5) but
nothing is built for them now.

---

## 2. Architecture at a glance

```
                 ┌───────────────────────────────────────┐
   HTTP request  │  AnalyticsMiddleware                  │
  ──────────────►│  • times the request                  │
                 │  • reads resolver_match.route         │
                 │  • counts SQL queries (execute_wrapper)│
                 └──────────────┬────────────────────────┘
                                │  Hit dataclass (not a model instance)
                                ▼
   optional JS   ┌───────────────────────────────────────┐
   beacon POST   │  In-process write buffer              │
  ──────────────►│  deque + background flush thread      │
   /_a/collect   │  flush on 100 rows or 5s, whichever   │
                 └──────────────┬────────────────────────┘
                                │  bulk_create(batch)
                                ▼
                 ┌───────────────────────────────────────┐
                 │  analytics_hit  (raw, partitioned by  │
                 │  month, 90-day retention)             │
                 └──────────────┬────────────────────────┘
                                │  nightly management command
                                ▼
                 ┌───────────────────────────────────────┐
                 │  analytics_daily_*  (rollups, kept    │
                 │  forever, tiny)                       │
                 └──────────────┬────────────────────────┘
                                ▼
                 ┌───────────────────────────────────────┐
                 │  Dashboard views / admin / query API  │
                 └───────────────────────────────────────┘
```

The important structural decision is the **two-tier storage split**: raw rows for recent
detail, permanent aggregates for history. Everything else follows from it.

---

## 3. Identity without cookies

This is the decision that determines whether you need a consent banner, so it comes first.

### The scheme

```python
visitor_hash = sha256(daily_salt + site_domain + ip_address + user_agent)[:16]
```

- The **salt rotates every 24 hours** and the old salt is destroyed.
- The **raw IP and full user-agent are never written to the database.** They exist only in
  memory during the request. What gets stored is the 16-byte hash, plus coarse derived
  values (browser family, OS family, device class, ISO country code).
- Because the salt is gone after 24 hours, the same person on the same device produces a
  different hash tomorrow. There is no mechanism, even for you with full database access,
  to link a visitor across days.

This is the same construction [Plausible](https://plausible.io/data-policy) uses, and it is
well-trodden ground for GDPR purposes.

### What you gain and what you give up

**Gain:** no cookie, so no cookie banner under the ePrivacy Directive; no PII at rest, which
takes the whole thing largely outside UK GDPR's heaviest obligations; nothing to include in
a data subject access request, because there is no stored identifier that can be tied back
to a person.

**Give up:** returning-visitor metrics across days, and any multi-day journey analysis.
Every day, everyone is new. "Unique visitors" means "unique visitors today", and summing
daily uniques over a month **overcounts** monthly uniques — the dashboard must say
"daily uniques" and never silently add them up. This is a real limitation and you should
decide now whether you can live with it.

> **Open decision A.** If multi-day returning-visitor data matters to you, the alternative
> is a first-party cookie with a random ID, which requires consent in the UK/EU and brings
> the banner back. A middle path: cookieless by default, with a `RETURNING_VISITORS`
> setting that opts into a consented cookie for sites that want it. I'd default to
> cookieless and ship the cookie path as opt-in.

### Sessions

A session is a run of hits from the same `visitor_hash` with no gap longer than 30 minutes,
bounded by the day (since the hash resets at midnight UTC anyway). Rather than compute this
at query time, the write path keeps a short-lived cache entry per visitor hash holding the
current `session_id` and last-seen timestamp; a hit more than 30 minutes after the last one
starts a new session. Cache miss just means a new session, which is the correct fallback.

### Logged-in users

Separately and deliberately: if the request has an authenticated user, store
`user_id` on the hit as a **nullable FK**, gated behind a setting that defaults to off.
This is a different privacy posture — it's identified, first-party, and covered by your
existing account terms rather than by anonymous-analytics reasoning. Keeping it a separate
opt-in field rather than folding it into `visitor_hash` means the anonymous path stays
clean and defensible.

---

## 4. Data model

### 4.1 `Hit` — the raw table

One row per request. This is the only high-volume table.

| Column | Type | Notes |
|---|---|---|
| `id` | `bigint` PK | |
| `ts` | `timestamptz` | partition key |
| `visitor_hash` | `bytea(16)` | §3 |
| `session_id` | `bytea(16)` | |
| `is_new_session` | `bool` | denormalised so entry-page queries don't self-join |
| `path` | `varchar(255)` | normalised, query string stripped (§4.2) |
| `route` | `varchar(255)` | `resolver_match.route`, e.g. `orders/<int:pk>/` |
| `view_name` | `varchar(255)` | `resolver_match.view_name` |
| `method` | `smallint` | enum |
| `status` | `smallint` | HTTP status code |
| `duration_ms` | `integer` | server-side wall time |
| `query_count` | `smallint` | SQL queries issued (§6.3) |
| `query_ms` | `integer` | time in the database |
| `referrer_host` | `varchar(128)` | host only, never full referrer URL |
| `referrer_path` | `varchar(255)` | nullable, off by default |
| `utm_source/medium/campaign` | `varchar(64)` | nullable |
| `country` | `char(2)` | ISO 3166-1, resolved at ingest |
| `region` | `varchar(64)` | nullable, off by default |
| `device` | `smallint` | enum: desktop / mobile / tablet / bot / other |
| `browser` | `smallint` | enum FK to a small lookup |
| `os` | `smallint` | enum FK to a small lookup |
| `is_bot` | `bool` | flagged, not dropped (§6.4) |
| `user_id` | `bigint` FK null | opt-in, §3 |
| `screen_w` | `smallint` null | beacon only |
| `props` | `jsonb` null | escape hatch, §4.5 |

**Indexes.** `(ts)` for partition-local scans, `(visitor_hash, ts)` for session assembly,
`(route, ts)` for the per-endpoint performance views. Deliberately *not* an index on
`path` — the rollups answer path questions, and a third index on the hot write table is a
meaningful insert cost at 5M/month.

`referrer_path` and `region` default to off because they're the two fields most likely to
turn "anonymous aggregate data" into something a regulator finds interesting, and neither
earns its keep for most sites.

### 4.2 Path normalisation

Path cardinality is what makes homegrown analytics tables blow up. Three rules at ingest:

1. Strip the query string entirely, except `utm_*` params which are promoted to their own
   columns.
2. Strip trailing slashes inconsistencies and lowercase the host.
3. Truncate at 255 chars.

`route` is the field you should actually build reports on. `path` is kept for the cases
where the specific URL matters (blog posts, product pages) and for debugging.

Two details on `route` worth knowing, since the whole §1 argument rests on it. It's been
available as `ResolverMatch.route` since Django 2.2, and for `include()`d URLconfs it holds
the **full concatenated route**, not just the leaf pattern — which is what you want.
`request.resolver_match` is `None` when no view resolved (404s, or a middleware that
short-circuited before resolution), so the collector must handle `None` and store
`route = ""`, relying on `status` to find those.

### 4.3 Rollup tables

Written nightly, kept forever, small enough to query without thinking.

**`DailyPageStat`** — grain: `(date, route, path)`
`views`, `visitors` (HLL or exact-distinct, see below), `sessions`, `entries`, `exits`,
`bounces`, `total_duration_ms`, plus response-time histogram buckets (§4.4).

**`DailySourceStat`** — grain: `(date, referrer_host, utm_source, utm_medium, utm_campaign)`
`sessions`, `visitors`, `bounces`.

**`DailyGeoDeviceStat`** — grain: `(date, country, device, browser, os)`
`views`, `visitors`, `sessions`.

**`DailyStatusStat`** — grain: `(date, route, status)`
`count`, response-time buckets. This is the errors table.

Kept as separate narrow tables rather than one wide fact table because the dimension
combinations multiply: one combined table at `(date, route, path, referrer, utm×3, country,
device, browser, os, status)` grain would have nearly as many rows as the raw table and
defeat the entire purpose.

**Distinct visitors in rollups** is the awkward one — counts of distinct things don't sum.
Store exact `visitors` per row (correct for that row, never summable across rows) *and*
maintain a `DailyUniqueVisitor` table of `(date, visitor_hash)` so that "unique visitors
over this range" is exact at any grouping.

Be aware this table is not as cheap as it looks. At 5M views/month you're looking at maybe
40k–150k visitor-days per day; at ~78 bytes per row all-in, that's **3–12 MB/day, or
1.1–4.3 GB/year** — the same order as the raw table it's supposed to be cheaper than.
Three options, and this is worth a decision:

- **Prune it** to a rolling 24 months and accept that older ranges fall back to
  non-summable per-row counts. Simplest.
- **Postgres `hll` extension** — store a HyperLogLog sketch per rollup row instead. Sketches
  *are* unionable, so distinct counts compose correctly across any grouping, at ~1–2 KB per
  row and ~2% error. Technically the right answer, at the cost of a Postgres extension the
  installer has to have.
- **Drop exact cross-range uniques entirely** and only ever report daily uniques. Honest,
  and arguably what a cookieless design implies anyway (§3).

### 4.4 Response-time histograms — the trick that makes perf data survive retention

Percentiles cannot be averaged or summed, so once the raw rows are pruned, a stored
`avg_duration_ms` tells you almost nothing (averages hide exactly the tail you care about).

Solution: store **fixed histogram buckets** in the rollup rows —
`≤25ms, ≤50, ≤100, ≤250, ≤500, ≤1000, ≤2500, ≤5000, >5000` as nine small integer columns.
Buckets *are* summable, so you can compute an approximate p50/p95/p99 for any route, over
any date range, forever, from data that costs 18 bytes per rollup row. Within the raw
retention window you can compute exact percentiles with `percentile_cont`; beyond it you
get the histogram approximation, which is within a bucket width and entirely good enough
for "is this endpoint getting slower".

Bucket boundaries must be a setting, because the right buckets for an API are not the right
buckets for a server-rendered site.

### 4.5 The `props` jsonb escape hatch

A nullable `jsonb` column on `Hit`, unused by v1, unindexed. Costs nothing when null.
It exists so that when you inevitably want to attach one project-specific field to hits,
you can do it without a migration on a 20-million-row table — and so that custom business
events can be layered on later without redesigning the schema.

---

## 5. Storage and scale — the actual numbers

Doing this arithmetic up front, because "will this eat my database" is the question that
decides whether the design is viable.

**Per raw row:** ~195 bytes of column data, plus ~28 bytes of Postgres row overhead (the
23-byte heap tuple header, MAXALIGN padding to 24 on 64-bit, and a 4-byte line pointer) →
**~225 bytes heap**. Three indexes at ~40 bytes/entry → **~120 bytes of index**. Call it
**~345 bytes per hit**; budget 350.

| Volume | Rows/year | Raw, unbounded | Raw @ 90-day retention |
|---|---|---|---|
| 1M views/mo | 12M | ~4.2 GB/yr | ~1.05 GB steady |
| 5M views/mo | 60M | ~21 GB/yr | ~5.3 GB steady |

Rollups themselves add roughly 1–5 MB/month regardless of traffic, dominated by route
cardinality. `DailyUniqueVisitor` is the one that isn't negligible — 1.1–4.3 GB/year at the
top of the range (§4.3).

**So: with 90-day raw retention, a 5M/month site sits at roughly 6–7 GB in year one,
growing 1–4 GB/year thereafter from the visitor table alone unless you prune it or switch
to HLL sketches.** Either way this is completely unremarkable for Postgres — the design
isn't close to any interesting limit.

**Write rate** is the pleasant surprise. 5M/month is 167k/day, which averages **1.9 writes
per second**. Even assuming traffic compresses into eight busy hours with 5× spikes, you
peak somewhere around **15–30 inserts/second**. Postgres does that without noticing.

This matters because it tells you what *not* to build: **you do not need Celery, Redis
Streams, Kafka, or a separate analytics database.** Recommending a queue here would be
cargo-culting a solution to a problem you don't have for another order of magnitude. The
in-process buffer in §6.2 is sufficient, and §10 lists the upgrade path for if that ever
changes.

**Lever if storage does become a problem:** normalising `path` and `referrer_host` into
lookup tables with integer FKs saves ~55 bytes/row, about 17%. Not worth the join complexity
at these volumes; worth it at 50M/month.

---

## 6. Collection pipeline

### 6.1 Two collectors, different jobs

**`AnalyticsMiddleware`** is the primary collector. It sees every server-rendered request,
requires no JavaScript, cannot be blocked by an ad blocker, and is the only thing that can
measure server-side timing. It's the source of truth for performance data.

**The beacon endpoint** (`POST /_a/collect`, ~1KB of vanilla JS) is optional and secondary.
It catches SPA route changes that never hit the server, and adds screen size and
client-side navigation timing. Because it's on your own domain it isn't blocked the way a
request to `google-analytics.com` is. If your project is server-rendered Django templates,
**you may not need it at all** — and shipping without it means shipping no JavaScript,
which is a nice property.

> **Open decision B.** Do you have SPA-ish frontends where server-side middleware would
> miss navigation? If everything is server-rendered, I'd drop the beacon from v1 entirely
> and halve the surface area.

Both collectors converge on the same internal `Hit` dataclass and the same buffer, so
there's one write path, not two.

One structural note on the middleware: it must do its work *after* calling
`self.get_response(request)`, because `request.resolver_match` is only populated once URL
resolution has happened. Django's docs are explicit that `resolver_match` is unavailable in
middleware running before resolution — but the handler sets it on the same request object
before invoking the view, so reading it on the way back out is both correct and documented.

### 6.2 The write path

The requirement is that analytics never adds a synchronous database write to the request
path, because that write lands squarely in your p99 latency.

```
Middleware builds a Hit dataclass (plain object, no ORM, no DB)
   → appends to a bounded deque
      → background daemon thread flushes on whichever comes first:
           • 100 buffered rows
           • 5 seconds elapsed
      → Hit.objects.bulk_create(batch)
```

Properties that matter:

- **The request path does zero I/O.** Appending to a deque is nanoseconds.
- **The deque is bounded** (default 10,000). On overflow it drops the *oldest* rows and
  increments a counter that's exposed in the dashboard. Analytics must never be able to
  take down the site, and a bounded buffer with visible loss is far better than an
  unbounded one with an OOM.
- **The flush thread swallows and logs exceptions.** A database blip loses a batch of
  analytics; it does not raise into user requests.
- **Flush on shutdown** via `atexit` plus a signal handler, so a graceful deploy doesn't
  lose the last five seconds.

**The multi-process caveat, stated plainly:** each Gunicorn/uWSGI worker has its own buffer
and its own flush thread. With 8 workers that's up to 8 concurrent `bulk_create` calls
every 5 seconds. At the volumes in §5 this is fine — it's a handful of batched inserts per
second. It is worth being explicit that this is the mechanism, because it's also exactly
the thing that stops working if you get 50× busier.

**Async note:** the middleware should declare both `sync_capable = True` (the default) and
`async_capable = True` (which defaults to *False*). With both set, Django passes
`get_response` through unadapted — no `sync_to_async`/`async_to_sync` thread-pool hop — and
the middleware branches on `iscoroutinefunction(get_response)` in `__init__`. Since the
buffer append is non-blocking there's no genuine async work to do, so this is cheap to get
right and expensive to leave out: an `async_capable = False` middleware forces every request
in an ASGI deployment through a thread pool.

### 6.3 Capturing query counts safely

`connection.queries` is the obvious-looking approach and the wrong one: it's only populated
when `DEBUG=True` (strictly, `force_debug_cursor or settings.DEBUG`), and it's capped at the
last 9,000 queries per connection — so it's both unavailable in production and lossy.

The correct API is **`connection.execute_wrapper()`**, a documented database-instrumentation
context manager that works independently of `DEBUG`. A wrapper has the signature
`wrapper(execute, sql, params, many, context)` and must call and return
`execute(sql, params, many, context)`. Wrappers are thread-local to the connection and
scoped to the `with` block, which suits this perfectly: the middleware wraps its
`self.get_response(request)` call in `execute_wrapper`, incrementing a counter and
accumulating elapsed time for exactly that request on exactly that thread.

This should be behind a setting defaulting to **on** but easy to disable, since it adds a
small per-query overhead. It's the single most useful field for finding N+1 problems — a
route averaging 340 queries per request is instantly obvious in a way that response time
alone never makes it.

### 6.4 Bot filtering

Bots are typically 30–50% of raw hits. If you don't handle them your numbers are wrong and
you'll stop trusting the dashboard within a month.

Three signals, applied at ingest:

1. **User-agent matching** against a maintained list. Ship a reasonable default list; make
   it extensible via settings.
2. **Known crawler IP ranges** for the big ones, optional.
3. **Behavioural**, if the beacon is enabled: a server hit with no corresponding beacon is
   suspicious (though also true for JS-disabled humans and prefetches, so it's a weak signal
   — use it to *flag*, never to *drop*).

Critically: **flag with `is_bot`, do not discard.** Dashboards filter bots out by default,
but keeping the rows means you can audit whether the filter is right, and recover if it's
wrong. Discarded data is gone forever; a wrong boolean is a backfill.

### 6.5 Exclusions

Skipped entirely, by default and configurable: static and media URLs, the admin,
health-check endpoints, the analytics app's own URLs, `HEAD` requests, and requests from
staff users. Also skip anything matching an `EXCLUDE_PATHS` regex list, because there is
always one weird endpoint.

---

## 7. Partitioning and retention

At 60M rows/year, `DELETE FROM analytics_hit WHERE ts < ...` is a long-running,
lock-holding, vacuum-generating operation you'd rather not run monthly. Range partitioning
by month turns retention into `DROP TABLE`, which is instant.

Django has no native declarative-partitioning support, so there are two routes:

- **`django-postgres-extra` (psqlextra)**, which provides `PostgresPartitionedModel` with a
  nested `PartitioningMeta`, and handles partition creation inside migrations.
- **Raw SQL in a `RunSQL` migration**, plus a small management command (or `pg_partman`) to
  create next month's partition ahead of time. No dependency, a few more moving parts.

**Take the raw SQL route.** Two reasons, and the second is decisive: a reusable package
taking a hard dependency on psqlextra is a big ask of anyone installing it, and psqlextra's
current stable release (2.0.9, July 2025) pins `Django<6.0` — it does not declare support
for Django 6.x at all, and its 3.0.0rc1 has been sitting as a pre-release since October
2024. Depending on it would cap the package at Django 5.x. The SQL involved is about 15
lines.

Django itself has no built-in declarative partitioning as of 6.1, and the upstream ticket
for it is blocked on composite-primary-key work, so this isn't something to wait for.

**Retention**: `RAW_RETENTION_DAYS` defaulting to 90. The nightly job rolls up, then drops
partitions entirely outside the window. Rollups are never pruned.

**This makes the package Postgres-first.** It would run on MySQL or SQLite without
partitioning (falling back to chunked deletes), but the partitioning, `jsonb`, and
`percentile_cont` paths are Postgres. Given the target volume I think Postgres-first with a
degraded fallback is the honest position, rather than pretending at database neutrality.

---

## 8. Reporting layer

### Query API

The main deliverable is a small, stable Python API over the rollups, so you can build
whatever reporting you want without hand-writing aggregate queries:

```python
from django_analytics.query import Report

Report.pageviews(start, end, group_by="route", limit=20)
Report.visitors(start, end, granularity="day")
Report.sources(start, end)
Report.performance(start, end, route="orders/<int:pk>/")   # p50/p95/p99 + histogram
Report.errors(start, end, status__gte=400)
```

Each returns plain dicts or a small result object, reads from rollups when the range is
fully historical, from raw rows when it includes today, and unions the two when it
straddles the boundary. That union logic is the fiddliest part of the whole package and
should be where the tests are densest.

### Dashboard

A handful of server-rendered Django views behind a `staff_member_required`-style
permission, with charts. Deliberately not a SPA — this is an internal tool, and a
dependency-free server-rendered dashboard is far easier for someone to install and trust
than one requiring a frontend build.

Suggested pages: Overview (traffic over time, top routes, top sources), Pages
(per-route detail), Sources, Performance (slowest routes, p95 trend, query counts),
Errors (4xx/5xx by route over time).

Django admin registration for the models is provided but is not the primary interface;
admin's list views are the wrong tool for aggregate reporting.

---

## 9. Package structure

```
django-<name>/
├── pyproject.toml
├── README.md
├── src/django_analytics/
│   ├── apps.py                # starts the flush thread on ready()
│   ├── conf.py                # settings with defaults + validation
│   ├── middleware.py
│   ├── models.py              # Hit + rollups
│   ├── buffer.py              # deque + flush thread
│   ├── identity.py            # salt rotation, visitor hashing, sessions
│   ├── enrich.py              # UA parsing, geo lookup, bot detection
│   ├── normalise.py           # path/referrer normalisation
│   ├── rollup.py              # raw → daily aggregation
│   ├── query.py               # the Report API
│   ├── views.py / urls.py     # beacon endpoint + dashboard
│   ├── admin.py
│   ├── static/…/beacon.js
│   ├── templates/…
│   ├── management/commands/
│   │   ├── analytics_rollup.py
│   │   ├── analytics_prune.py
│   │   ├── analytics_partitions.py
│   │   └── analytics_rotate_salt.py
│   └── migrations/
└── tests/
```

### Settings

One namespaced dict, validated at startup with clear errors:

```python
ANALYTICS = {
    "ENABLED": True,
    "TRACK_AUTHENTICATED_USER_ID": False,
    "BEACON_ENABLED": False,
    "RAW_RETENTION_DAYS": 90,
    "SESSION_TIMEOUT_MINUTES": 30,
    "EXCLUDE_PATHS": [r"^/static/", r"^/admin/", r"^/healthz"],
    "EXCLUDE_STAFF": True,
    "TRACK_QUERY_COUNTS": True,
    "DURATION_BUCKETS_MS": [25, 50, 100, 250, 500, 1000, 2500, 5000],
    "BOT_UA_PATTERNS": [...],
    "GEOIP_ENABLED": False,
    "BUFFER_MAX": 10_000,
    "FLUSH_EVERY_ROWS": 100,
    "FLUSH_EVERY_SECONDS": 5,
    "DATABASE_ALIAS": "default",
}
```

`DATABASE_ALIAS` is there from day one even though it'll be `"default"` for years — it
costs nothing now and it's the seam that lets you move analytics to its own database later
without touching call sites.

### Dependencies

`Django >= 5.2` (the current LTS; note Django moves to annual releases from 2028 and the
LTS designation goes away, so target 5.2/6.x and don't build anything version-fragile).
`user-agents` for UA parsing. Optionally `geoip2` + MaxMind's free GeoLite2 database, off by
default so installation doesn't require a MaxMind account. Nothing else — the value of a
reusable package drops sharply with each dependency an installer has to accept.

---

## 10. Deliberate upgrade paths

The design should be simple now but not a dead end. Three seams are built in:

| If this happens | You change this | Nothing else moves |
|---|---|---|
| Write volume outgrows the in-process buffer | Swap `buffer.py` for a Redis list or Celery task | Middleware and models unchanged |
| Analytics queries start affecting app performance | Point `DATABASE_ALIAS` at a replica or separate DB | Call sites unchanged |
| Raw volume outgrows Postgres | Sample raw rows (keep counters exact) or ship raw to ClickHouse, keeping rollups in Postgres | The `Report` API is already rollup-first |

The `Report` API being the only public read surface is what makes the third one possible.

---

## 11. Prior art

Worth knowing what exists, mostly to confirm the gap is real:

- **`django-tracking2`, `django-tracking-analyzer`** — the closest existing packages, both
  essentially unmaintained and predating modern Django. Neither does rollups, retention, or
  performance data.
- **`django-analytical`** — a snippet injector for third-party services. Different problem.
- **Plausible, Umami, Fathom, Matomo** — good self-hosted products, but they're separate
  services with their own database and deployment. They can't join to your models, can't see
  Django routes, and can't measure server-side timing. The whole argument for building this
  is that living *inside* the framework gives you the three things in §1 that a separate
  service structurally cannot.

---

## 12. Build order

Roughly a week of focused work to something useful, with the first two stages being ~80% of
the value:

1. **Core** — models, migrations, middleware, buffer, identity, normalisation, exclusions.
   Usable at this point: raw data is landing.
2. **Rollups and retention** — the nightly command, partitioning, pruning, salt rotation.
   Now it's sustainable.
3. **Reporting** — `Report` API and dashboard views. Now it's pleasant.
4. **Enrichment** — geo, better UA parsing, bot list refinement.
5. **Beacon** — only if §6 open decision B says you need it.
6. **Packaging** — pyproject, docs, test matrix, publish.

---

## 13. Decisions I need from you

1. **Cookieless only, or optional consented cookie for returning visitors?** (§3) — this
   is the one with real product consequences.
2. **Do you need the JS beacon, or is everything server-rendered?** (§6.1) — dropping it
   removes a meaningful chunk of surface area.
3. **Postgres-first with a degraded fallback, or genuine database neutrality?** (§7) — I
   recommend the former.
4. **Retention window.** 90 days of raw is my default; 30 makes it tiny, 365 makes it ~21GB.
5. **Package name.** `django-sitepulse`, `django-tracer`, `django-owndata`, or something of
   yours. Worth checking PyPI before committing.
6. **Publish publicly, or internal-only?** Affects how much effort goes into docs, the
   supported-versions test matrix, and how conservative the settings API needs to be.

---

## Sources

- [Django download and supported versions](https://www.djangoproject.com/download/)
- [Django is moving to an annual release cycle](https://www.djangoproject.com/weblog/2026/aug/10/annual-release-cycle/)
- [Plausible data policy — cookieless visitor identification](https://plausible.io/data-policy)
- [PostgreSQL table partitioning documentation](https://www.postgresql.org/docs/current/ddl-partitioning.html)
- [django-tracking2](https://github.com/bruth/django-tracking2)
- [Django Packages — analytics grid](https://djangopackages.org/grids/g/analytics/)
