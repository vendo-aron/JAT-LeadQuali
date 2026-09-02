# Local database

Development runs the same engine as production — Postgres 16 in Docker locally, RDS in AWS —
so nothing about Postgres is discovered for the first time at deploy time.

## 1. Start Postgres

```bash
docker compose up -d
```

`docker-compose.yml` defines one service, `postgres`, published on `localhost:5432` with a
named volume for its data. It has a real healthcheck (`pg_isready`), because
`alembic upgrade head` run immediately after `up -d` fails against a server that is still
initialising. Wait for it rather than sleeping:

```bash
# Blocks until the container reports healthy.
docker compose ps --format '{{.Health}}'
until [ "$(docker inspect -f '{{.State.Health.Status}}' leadquali-postgres)" = healthy ]; do sleep 1; done
```

On Windows PowerShell:

```powershell
while ((docker inspect -f '{{.State.Health.Status}}' leadquali-postgres) -ne 'healthy') { Start-Sleep 1 }
```

## 2. Point the app at it

The database URL lives in **one** place: the `DATABASE_URL` environment variable, read
through `leadquali.config.Settings`. It is deliberately absent from `alembic.ini`, so no
connection string can be committed and there is no second way to aim a process at a
database.

Copy the example file once:

```bash
cp .env.example .env      # already contains the docker-compose credentials
```

…or export it for the shell:

```bash
export DATABASE_URL='postgresql+psycopg://leadquali:leadquali@localhost:5432/leadquali'
```

The credentials are local throwaways that match `docker-compose.yml`. Real ones come from
Secrets Manager and never appear in a file.

## 3. Run the migrations

```bash
alembic upgrade head
```

Useful neighbours:

| Command | What it does |
|---|---|
| `alembic current` | Which revision this database is on. |
| `alembic history` | The revision graph, newest first. |
| `alembic downgrade base` | Drop every table this project owns. |
| `alembic upgrade head --sql` | Print the SQL instead of running it — for a review by a DBA. |
| `alembic check` | Fail if the models and the migrations have drifted. |

### Changing the schema

Edit `src/leadquali/adapters/db_schema.py` first — it is the source of truth — then let
Alembic write the migration:

```bash
alembic revision --autogenerate -m "add whatever"
```

Read what it produced before committing it. Autogenerate does not detect everything (table
and column *renames* come out as a drop plus an add, which loses data), and it does not know
that a new `NOT NULL` column on a populated table needs a backfill.

Then confirm there is nothing left over:

```bash
alembic upgrade head
alembic check        # must print "No new upgrade operations detected."
```

A migration that no longer describes the models is worse than no migration: the application
reads columns the database does not have, and production is where you find out.
`tests/integration/test_migrations.py::test_autogenerate_against_head_produces_an_empty_diff`
asserts this, so CI catches the drift if you forget.

A freshly migrated database is not yet a usable one — continue to step 4.

### What the schema will not let you do

Three refusals are deliberate, and worth recognising before you assume something is broken:

- **`DELETE FROM tenants` fails while the tenant has leads.** The tenant foreign key is
  `ON DELETE RESTRICT`. Cascading from a tenant would let one over-broad `WHERE` destroy
  every lead, assessment, routing event and feedback row a customer ever had — including
  the audit trail that exists to prove no lead was dropped. To remove a tenant, delete its
  leads first (that *does* cascade to the child tables), then the tenant. #37's erasure
  routine is the supported way to do it.
- **An `assessments` row must be wholly a success or wholly a failure.** `status = 'ok'`
  requires `dimension_scores`, `extracted`, `reasoning`, `confidence`, `tier` and
  `total_score`; `status = 'failed'` requires all of them to be absent and an
  `escalation_reason` to be present. A failed run is a real, recordable outcome — a lead
  the system could not assess is escalated, never dropped — but a half-written one is not.
  Note that `escalation_reason = 'low_confidence'` accompanies a *successful* assessment:
  the model answered, code did not trust the answer.
- **`INSERT INTO tenants` fails without `icp_config`.** See step 4.

`feedback.rater` deserves a mention here too, because the database cannot enforce it: it is
an **opaque subject id** — an internal user id, or a hash of one. Not an email address and
not a display name. It is grouped by and joined in the plan §4 analytics and is retained
for as long as the feedback is useful, which outlives the raw lead payload, so putting a
person's address in it would place personal data outside `leads.raw_payload` — the one
place CLAUDE.md invariant 5 allows it to live.

## 4. Seed the default tenant

```bash
python scripts/seed.py
```

`alembic upgrade head` gives you five empty tables and nothing that can accept a lead. Every
row in the system hangs off a tenant, and `tenants.icp_config` — the rubric — has **no
server default**, precisely so that a tenant cannot exist without one. Seeding is therefore
not a convenience step: it is the second half of creating the database.

The script reads `tenants/default.json` (issue #8's file, which is also what the Phase 1
config loader reads) and upserts it as the internal tenant:

| Flag | Default | What it does |
|---|---|---|
| `--config PATH` | `tenants/default.json` | The tenant config JSON to seed from. |
| `--database-url URL` | `$DATABASE_URL` | Where to write. Same source Alembic reads. |

It is **idempotent**. The tenant's primary key is derived from the config's `tenant_id`
slug (a UUID5), so re-running the script updates the tenant it created last time instead of
adding a second one — and the default tenant has the same id in every developer's database,
which is what makes a fixture or a support query portable. Run it again after editing
`tenants/default.json` to push the change into the database.

If `tenants/default.json` is not in your checkout yet, the script says so and exits 1
rather than seeding something half-formed; pass `--config` to point at a file you do have.

> **Note on validation.** `scripts/seed.py` checks the file's *structure* — the rubric keys
> are present and roughly the right kind — rather than validating it against `TenantConfig`.
> `TenantConfig` lives on issue #8's branch and cannot be imported here yet. Once #8 and #15
> are both on the default branch, that check should be replaced by a single
> `TenantConfig.model_validate(document)` call, so there is one validator rather than two
> that can disagree. `leadquali.adapters.seed` says the same thing in its module docstring.

## 5. Run the integration tests

```bash
pytest -m integration
```

These are the tests that need a real server: every table and column exists, the
`(tenant_id, submission_id)` unique constraint really rejects a redelivered submission, a
child row whose `tenant_id` disagrees with its lead's owner is rejected, a failed assessment
can be recorded while a half-written one cannot, seeding is idempotent, the foreign keys
cascade and restrict as intended, and `downgrade base` → `upgrade head` round-trips.

Two things worth knowing about how they run:

- **They never touch the database in `DATABASE_URL`.** Each session creates a throwaway
  `leadquali_test` (and a second, uniquely-named database for the downgrade round-trip),
  migrates it, and drops it afterwards. `alembic downgrade base` is one of the things under
  test, and running that against your working database would delete your data.
- **They skip, they never fail, when there is no database.** `DATABASE_URL` unset, Docker
  not running, or nothing listening on the port all produce a skip with the reason attached.
  A red bar on a laptop with no Docker would say "the schema is broken" when it only means
  "Postgres is not up", so the default suite and CI stay green either way.

To watch them skip on purpose:

```bash
env -u DATABASE_URL pytest -m integration -rs
```

The offline half of the schema tests — multi-tenancy on every table, the idempotency key,
no raw-email column, index coverage — is in `tests/unit/test_db_schema.py` and needs
nothing running.

## 6. Poke at it by hand

```bash
docker compose exec postgres psql -U leadquali -d leadquali
```

```
\dt              -- tables
\d leads         -- one table's columns, indexes and constraints
\di              -- every index
```

## 7. Stop it

```bash
docker compose down       # stop the container, keep the data
docker compose down -v    # ...and throw the data away
```

`down -v` is the fix for "my database is in a state I do not understand": drop the volume,
`up -d` again, `alembic upgrade head`, and you are on a schema that matches the models
exactly.

## Troubleshooting

**`port is already allocated`** — something else is on 5432, often a Postgres installed
natively. Stop it, or change the published port in `docker-compose.yml` and in
`DATABASE_URL` to match.

**`RuntimeError: DATABASE_URL is not set`** — `alembic` was run without the variable and
without a `.env` in the working directory. This error is deliberate: the alternative is a
default that silently migrates the wrong database.

**`FATAL: database "leadquali" does not exist`** — the volume was created before
`POSTGRES_DB` was set. `docker compose down -v && docker compose up -d`.

**`connection refused` right after `up -d`** — the healthcheck has not passed yet. Wait for
it (step 1) instead of retrying by hand.

**`null value in column "icp_config" ... violates not-null constraint`** — a tenant is being
created without a rubric. That is the constraint working; supply one, or seed with
`python scripts/seed.py` (step 4).

**`update or delete on table "tenants" violates foreign key constraint
"fk_leads_tenant_id_tenants"`** — the tenant still owns leads and the foreign key restricts.
Delete the leads first, then the tenant. See "What the schema will not let you do".

**`new row ... violates check constraint "ck_assessments_output_present_iff_ok"`** — the
assessment being written is neither a complete success nor a clean failure. Either supply
every model-output column with `status = 'ok'`, or none of them with `status = 'failed'`
and an `escalation_reason`.

**`seed: tenant config file not found`** — `tenants/default.json` ships with issue #8. Pass
`--config` to point the seed script at a config you do have.
