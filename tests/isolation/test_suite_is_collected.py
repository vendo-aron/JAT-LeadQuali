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


def _collapse_not(terms: list[str]) -> list[str]:
    """Join ``not`` to the term it negates, so a bare term is visible as selection."""
    collapsed: list[str] = []
    index = 0
    while index < len(terms):
        if terms[index] == "not" and index + 1 < len(terms):
            collapsed.append(f"not {terms[index + 1]}")
            index += 2
        else:
            collapsed.append(terms[index])
            index += 1
    return collapsed


def narrowing_arguments(arguments: list[str]) -> list[str]:
    """The arguments in a ``pytest`` invocation that could drop this directory from a run.

    A path argument, ``--ignore`` or ``-k`` can, and those are refused outright.

    ``-m`` is the one that needs thought, and getting it wrong in either direction is a
    real cost. Every module in this package is unmarked except the integration one, so a
    purely *negative* expression — ``not live_api and not integration`` — cannot exclude
    any of them, and it is the only way CI can skip the tests that need an Anthropic key or
    a database. Refusing it would force CI to either spend money or go red. A *positive*
    expression like ``-m unit`` is different: it selects, so every unmarked test in this
    directory silently disappears. That is the case this refuses.

    The earlier version of this check refused every ``-m``, which made it fire on #5's
    perfectly correct workflow the moment that branch landed. A guard that cries wolf at
    the right fix gets deleted by the next person, which would have cost the real
    protection below.
    """
    narrowing: list[str] = []
    skip_next = False
    for index, argument in enumerate(arguments):
        if skip_next:
            skip_next = False
            continue
        if (
            argument.startswith(("--ignore", "tests/"))
            or argument == "-k"
            or argument.startswith("-k")
        ):
            narrowing.append(argument)
            continue
        if argument == "-m" or argument.startswith("-m"):
            if argument == "-m":
                expression = arguments[index + 1] if index + 1 < len(arguments) else ""
                skip_next = True
            else:
                expression = argument[2:]
            terms = [
                term
                for term in expression.replace("(", " ").replace(")", " ").split()
                if term not in {"and", "or"}
            ]
            if any(not term.startswith("not ") for term in _collapse_not(terms)):
                narrowing.append(f"-m {expression}")
    return narrowing


@pytest.mark.parametrize(
    ("invocation", "refused"),
    [
        ("tests/unit tests/contract", True),
        ("--ignore=tests/isolation", True),
        ('-k "not isolation"', True),
        ('-m "unit"', True),
        ('-m "integration or isolation"', True),
        ('-m "not live_api and not integration"', False),
        ('-m "not integration"', False),
        ("-q --strict-markers", False),
        ("", False),
    ],
)
def test_the_narrowing_rule_refuses_selection_and_allows_exclusion(
    invocation: str, refused: bool
) -> None:
    """The rule this file's workflow scan rests on, tested rather than trusted.

    Both directions matter. Missing a narrowing argument silently drops this whole suite
    out of CI, which is the acceptance criterion. Refusing a legitimate one is how the
    check gets deleted by somebody whose correct workflow it rejected -- which is what
    happened the moment #5 landed, and why the rule now distinguishes selection from
    exclusion instead of refusing every ``-m``.
    """
    assert bool(narrowing_arguments(shlex.split(invocation))) is refused


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
                narrowing = narrowing_arguments(arguments)
                if narrowing:
                    offenders.append(f"{path.name}:{number}: pytest {' '.join(arguments)}")
    assert not offenders, (
        "a CI workflow narrows its pytest run, which can silently drop tests/isolation:\n"
        + "\n".join(offenders)
    )
