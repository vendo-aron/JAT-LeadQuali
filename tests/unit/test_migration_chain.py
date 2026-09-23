"""The migration chain is linear, and nothing in it is unreachable.

Alembic is happy to hold two heads. A stack of branches is exactly how you get them: two
issues each add a migration off the same parent, both are correct in isolation, and the
defect appears only once the branches are combined. ``alembic upgrade head`` then fails on
the merged trunk with "Multiple head revisions are present", after the merge, which is the
worst possible moment to find out.

This ran as a real failure while #35 was stacked on #36 -- two heads, both legitimate, and
no test to say so. It needs no database, so unlike ``tests/integration/test_migrations.py``
it runs everywhere.
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONS = REPO_ROOT / "migrations" / "versions"
_REVISION_RE = re.compile(r'^revision(?::\s*str)?\s*=\s*"([^"]+)"', re.MULTILINE)


def _scripts() -> ScriptDirectory:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def _revisions_on_disk() -> set[str]:
    found: set[str] = set()
    for path in VERSIONS.glob("*.py"):
        match = _REVISION_RE.search(path.read_text(encoding="utf-8"))
        if match is not None:
            found.add(match.group(1))
    return found


def test_there_is_exactly_one_head() -> None:
    """Two heads means two branches added a migration off the same parent."""
    heads = _scripts().get_heads()
    assert len(heads) == 1, (
        f"the migration chain has {len(heads)} heads ({', '.join(sorted(heads))}); "
        "re-point the later revision's down_revision at the other one"
    )


def test_every_revision_is_reachable_from_the_head() -> None:
    """A revision nobody points at is a migration that will never be applied.

    Walking back from the head has to visit every script on disk. A typo'd
    ``down_revision`` produces a file that exists, imports, passes every other test in the
    suite, and silently never runs.
    """
    scripts = _scripts()
    (head,) = scripts.get_heads()
    walked = {revision.revision for revision in scripts.walk_revisions("base", head)}
    on_disk = _revisions_on_disk()
    assert on_disk, "no migration scripts were found; the glob is wrong, not the chain"
    assert on_disk - walked == set(), f"unreachable from the head: {sorted(on_disk - walked)}"
