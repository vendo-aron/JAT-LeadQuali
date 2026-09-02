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

## 4. Run the integration tests

```bash
pytest -m integration
```

These are the tests that need a real server: every table and column exists, the
`(tenant_id, submission_id)` unique constraint really rejects a redelivered submission, the
foreign keys cascade, and `downgrade base` → `upgrade head` round-trips.

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

## 5. Poke at it by hand

```bash
docker compose exec postgres psql -U leadquali -d leadquali
```

```
\dt              -- tables
\d leads         -- one table's columns, indexes and constraints
\di              -- every index
```

## 6. Stop it

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
