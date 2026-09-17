"""The suite runs on every push, asserted rather than assumed.

The first acceptance criterion of #32 is that the isolation suite runs in CI on every push.
The way to make that true is not a CI job — a job is a thing somebody can forget, rename or
gate — but for the suite to be collected by the plain ``pytest`` invocation everything else
already uses. This module checks the two ways that could stop being true:

* ``testpaths`` in ``pyproject.toml`` no longer covers ``tests/isolation``;
* something — ``addopts``, a marker, a workflow's command line — filters it back out.

The CI workflow itself belongs to #5 (*[P0.4] CI: GitHub Actions running ruff, mypy and
pytest on every push*) and is not in the tree yet, so this module does not write one or
assert that one exists. What it does instead is scan every workflow file that *is* present
and refuse any ``pytest`` invocation that would exclude this directory, so the check starts
biting the moment #5 lands rather than needing to be remembered then.
"""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PYPROJECT: Final[Path] = REPO_ROOT / "pyproject.toml"
ISOLATION_DIR: Final[Path] = REPO_ROOT / "tests" / "isolation"

#: This module is excluded from its own source scans: it names ``pytestmark`` and
#: ``live_api`` as the things it is looking for, and a scan that matched itself would be a
#: permanent false positive.
SELF: Final[str] = Path(__file__).name


def isolation_modules() -> list[Path]:
    """Every test module in this package except this one."""
    return [path for path in sorted(ISOLATION_DIR.glob("test_*.py")) if path.name != SELF]


#: Where a GitHub Actions workflow lives once it is active, and where this repository stages
#: one while the push credential lacks the ``workflow`` scope (see the header of
#: ``ci/github-actions-eval.yml``).
WORKFLOW_DIRECTORIES: Final[tuple[Path, ...]] = (
    REPO_ROOT / ".github" / "workflows",
    REPO_ROOT / "ci",
)


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    document: dict[str, Any] = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return document


def _pytest_config(pyproject: dict[str, Any]) -> dict[str, Any]:
    section: dict[str, Any] = pyproject["tool"]["pytest"]["ini_options"]
    return section


def test_the_isolation_suite_is_inside_testpaths(pyproject: dict[str, Any]) -> None:
    """``pytest`` with no arguments collects this directory.

    Confirmed against the configuration rather than assumed from the fact that the file is
    under ``tests/``: ``testpaths`` could name ``tests/unit`` and ``tests/contract``
    individually, and this whole package would then run only when somebody pointed at it.
    """
    testpaths = [REPO_ROOT / entry for entry in _pytest_config(pyproject)["testpaths"]]
    assert testpaths, "testpaths is empty; a bare pytest would collect the whole repository"
    assert any(ISOLATION_DIR.is_relative_to(path) for path in testpaths), testpaths


def test_the_default_invocation_filters_nothing_out(pyproject: dict[str, Any]) -> None:
    """``addopts`` does not carry a marker expression.

    A ``-m "not integration"`` here would be reasonable-looking and would quietly turn the
    ``integration`` half of this suite from "skipped, and says so" into "never mentioned".
    Worse, a ``-m`` of any kind changes what ``tests/conftest.py`` does with ``live_api``.
    """
    addopts = shlex.split(str(_pytest_config(pyproject).get("addopts", "")))
    assert "-m" not in addopts, addopts
    assert not any(option.startswith("--ignore") for option in addopts), addopts
    assert not any(option.startswith("-k") for option in addopts), addopts


def test_the_only_marker_the_suite_hides_behind_is_integration() -> None:
    """Every isolation module runs by default except the one that needs a database.

    The rule this suite exists to enforce, applied to itself: a property asserted only by a
    skipped test is not asserted. So exactly one module here may be ``integration``-marked,
    and it is the one whose name says so.
    """
    marked = {
        path.name
        for path in isolation_modules()
        if "pytestmark" in path.read_text(encoding="utf-8")
    }
    assert marked == {"test_repository_isolation_integration.py"}, marked


def test_no_isolation_module_is_marked_live_api() -> None:
    """``live_api`` is skipped unless explicitly selected, so nothing here may carry it."""
    for path in isolation_modules():
        assert "live_api" not in path.read_text(encoding="utf-8"), path.name


def test_the_offline_half_of_the_suite_is_substantial() -> None:
    """A floor on how much of this package runs without Postgres.

    Not a count of tests — that would need updating every time one is added — but a count
    of *modules* that run unconditionally. Seven axes were asked for; six of them are
    provable without a database, and the seventh has an offline half in
    ``test_repository_isolation.py``.
    """
    modules = {path.name for path in ISOLATION_DIR.glob("test_*.py")}
    offline = modules - {"test_repository_isolation_integration.py"}
    assert len(offline) >= 6, sorted(modules)


def test_no_workflow_runs_pytest_in_a_way_that_would_skip_this_directory() -> None:
    """Scan whatever CI configuration exists for a filtered ``pytest``.

    Vacuous today — the CI workflow is #5's and has not landed — and deliberately written
    as a scan rather than as a test that skips when the file is missing, so that it applies
    itself to ``ci.yml`` the moment that file appears without anybody having to come back
    here. A workflow that runs ``pytest -m "not integration"`` or ``pytest tests/unit``
    would silently drop this suite out of CI, which is precisely the acceptance criterion.
    """
    offenders: list[str] = []
    for directory in WORKFLOW_DIRECTORIES:
        for path in sorted(directory.glob("*.yml")) if directory.is_dir() else ():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip().lstrip("- ").strip()
                if "pytest" not in stripped or stripped.startswith("#"):
                    continue
                arguments = shlex.split(stripped[stripped.index("pytest") + len("pytest") :])
                narrowing = [
                    argument
                    for argument in arguments
                    if argument in {"-m", "-k"}
                    or argument.startswith(("-m", "-k", "--ignore", "tests/"))
                ]
                if narrowing:
                    offenders.append(f"{path.name}:{number}: pytest {' '.join(arguments)}")
    assert not offenders, (
        "a CI workflow narrows its pytest run, which can silently drop tests/isolation:\n"
        + "\n".join(offenders)
    )
