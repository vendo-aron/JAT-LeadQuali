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

import os
import re
import shlex
import tomllib
from collections.abc import Iterator
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


def _selects_unmarked(expression: str) -> bool:
    """Whether a ``-m`` expression still selects a test that carries no markers.

    That is the whole property, and it is one evaluation rather than a grammar. Every module
    in this package is unmarked except the integration one, so an expression under which an
    unmarked test still runs cannot drop any of them; one under which it does not, does.

    The expression is evaluated with every marker name bound to ``False`` — which *is* an
    unmarked test — using Python's own boolean grammar, because pytest's marker expressions
    are a subset of it. No builtins are exposed and every name is pre-bound, so there is
    nothing for the ``eval`` to reach.

    ``not live_api and not integration`` → ``True``, allowed: CI has to be able to skip the
    tests that need an Anthropic key or a database, and refusing that would force CI to
    either spend money or go red. ``not (live_api or integration)`` → ``True`` as well,
    which the previous hand-written parser refused because it treated parentheses as
    whitespace — the same crying-wolf failure it was written to fix, in a different costume.
    ``unit`` → ``False``, refused: it selects, so every unmarked test here disappears.
    """
    names = set(re.findall(r"[A-Za-z_]\w*", expression)) - {"not", "and", "or"}
    try:
        return bool(eval(expression, {"__builtins__": {}}, dict.fromkeys(names, False)))  # noqa: S307
    except (SyntaxError, NameError, TypeError):
        # Not an expression we can reason about. Refusing is the safe direction: an
        # unparseable marker expression in CI deserves a human either way.
        return False


def _drops_this_directory(path_argument: str) -> bool:
    """Whether a positional path argument excludes ``tests/isolation`` from the run.

    Normalised rather than prefix-matched, because ``pytest ./tests/unit`` and
    ``pytest tests/unit`` are the same command and a ``startswith("tests/")`` rule sees only
    one of them. A path that contains this directory — ``tests``, ``.``, the repository root
    — keeps the suite; anything else drops it.
    """
    normalised = Path(os.path.normpath(path_argument))
    target = Path("tests/isolation")
    return not (target == normalised or target.is_relative_to(normalised))


def narrowing_arguments(arguments: list[str]) -> list[str]:
    """The arguments in a ``pytest`` invocation that could drop this directory from a run.

    ``--ignore``, ``--deselect`` and ``-k`` can, and are refused outright. A positional path
    is refused unless this directory is underneath it. ``-m`` is decided by
    :func:`_selects_unmarked`.

    Two directions of failure, and both are real. Missing a narrowing argument silently
    drops this whole suite out of CI, which is the acceptance criterion. Refusing a
    legitimate one is how the check gets deleted by somebody whose correct workflow it
    rejected — which is what happened the moment #5 landed.

    **Out of scope:** a marker expression reaching pytest some other way, most obviously a
    ``PYTEST_ADDOPTS`` in a workflow's ``env:`` block. This reads command lines, and the
    docstring says so rather than implying the coverage is complete.
    """
    narrowing: list[str] = []
    skip_next = False
    for index, argument in enumerate(arguments):
        if skip_next:
            skip_next = False
            continue
        if argument.startswith(("--ignore", "--deselect")) or argument.startswith("-k"):
            narrowing.append(argument)
            if argument in {"-k", "--ignore", "--deselect"}:
                skip_next = True
            continue
        if argument.startswith("-m"):
            if argument == "-m":
                expression = arguments[index + 1] if index + 1 < len(arguments) else ""
                skip_next = True
            else:
                expression = argument[2:]
            if not _selects_unmarked(expression):
                narrowing.append(f"-m {expression}")
            continue
        if not argument.startswith("-") and _drops_this_directory(argument):
            narrowing.append(argument)
    return narrowing


@pytest.mark.parametrize(
    ("invocation", "refused"),
    [
        # Paths that drop this directory, however they are spelled.
        ("tests/unit tests/contract", True),
        ("./tests/unit ./tests/contract", True),
        ("tests/unit/", True),
        ("--ignore=tests/isolation", True),
        ("--ignore tests/isolation", True),
        ("--deselect tests/isolation/test_log_isolation.py", True),
        ("--deselect=tests/isolation", True),
        ('-k "not isolation"', True),
        ("-k not_isolation", True),
        # Marker expressions that select rather than exclude.
        ('-m "unit"', True),
        ('-m "integration or isolation"', True),
        ('-m "not live_api and integration"', True),
        # Marker expressions under which an unmarked test still runs.
        ('-m "not live_api and not integration"', False),
        ('-m "not (live_api or integration)"', False),
        ('-m "not integration"', False),
        # Paths that keep this directory, and arguments that select nothing.
        ("tests", False),
        ("./tests", False),
        (".", False),
        ("tests/isolation", False),
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


#: ``PYTEST_ADDOPTS: -m unit`` in a workflow's ``env:`` block narrows every ``pytest`` in
#: that job without appearing on any command line. Scanned for the same way a command line
#: is, because a hole that is documented is still a hole.
_ADDOPTS: Final[re.Pattern[str]] = re.compile(r"PYTEST_ADDOPTS\s*:\s*(.*)$")


_RUN: Final[re.Pattern[str]] = re.compile(r"^run:\s*(.+)$")
"""A workflow step's command. Anything else on a line is prose, however it reads."""

_INVOKES_PYTEST: Final[re.Pattern[str]] = re.compile(r"(?:^|[\s;&|(])pytest(?:$|[\s;&|)])")
"""``pytest`` as a word being run, not as a substring of a filename or a sentence."""


def _pytest_invocations(text: str) -> Iterator[tuple[int, str, list[str]]]:
    """Every place a workflow file hands arguments to pytest, as ``(line, source, argv)``.

    Two shapes. A ``run:`` step that names ``pytest``, and a ``PYTEST_ADDOPTS`` environment
    variable, which reaches every pytest in the job without being written next to one.
    """
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip().lstrip("- ").strip()
        if stripped.startswith("#"):
            continue
        addopts = _ADDOPTS.search(stripped)
        if addopts is not None:
            value = addopts.group(1).strip().strip("'\"")
            yield number, "PYTEST_ADDOPTS", shlex.split(value)
            continue
        # Only a `run:` command counts. A line may mention pytest without invoking it —
        # `- name: Test (pytest, offline suite only)` is the one that caught this out, and
        # it scanned as `pytest , offline suite only)`, whose bare words then looked like
        # path arguments. A guard that fails on a step's own label is a guard somebody
        # deletes, which was the whole lesson of the -m over-strictness above.
        command = _RUN.match(stripped)
        if command is None:
            continue
        invocation = command.group(1).strip()
        if not _INVOKES_PYTEST.search(invocation):
            continue
        tail = invocation[invocation.index("pytest") + len("pytest") :]
        yield number, "pytest", shlex.split(tail)


def test_no_workflow_runs_pytest_in_a_way_that_would_skip_this_directory() -> None:
    """Scan whatever CI configuration exists for a narrowed ``pytest``.

    Written as a scan rather than as a test that skips when no workflow exists, so that it
    applies itself to a new one the moment it appears rather than needing to be remembered
    then. It has already done its job once: when #5 landed, CI ran
    ``pytest tests/unit tests/contract`` and this fired.

    What it reads is command lines and ``PYTEST_ADDOPTS``. A marker expression reaching
    pytest by some third route — a generated config, a wrapper script — is outside it, and
    ``narrowing_arguments`` says so rather than implying otherwise.
    """
    offenders: list[str] = []
    for directory in WORKFLOW_DIRECTORIES:
        for path in sorted(directory.glob("*.yml")) if directory.is_dir() else ():
            text = path.read_text(encoding="utf-8")
            for number, source, arguments in _pytest_invocations(text):
                narrowing = narrowing_arguments(arguments)
                if narrowing:
                    offenders.append(
                        f"{path.name}:{number}: {source} {' '.join(arguments)} "
                        f"— narrowed by {' '.join(narrowing)}"
                    )
    assert not offenders, (
        "a CI workflow narrows its pytest run, which can silently drop tests/isolation:\n"
        + "\n".join(offenders)
    )


def test_the_workflow_scan_reads_both_the_command_line_and_the_environment() -> None:
    """The scan's own parsing, on a workflow that does not exist.

    ``test_no_workflow_runs_pytest_in_a_way_that_would_skip_this_directory`` is vacuous
    whenever the repository has no offending workflow, which is most of the time — so what
    it would find is asserted here rather than left until a real one appears.
    """
    workflow = """
jobs:
  test:
    env:
      PYTEST_ADDOPTS: -m unit
    steps:
      - name: Test (pytest, offline suite only)
        run: pytest tests/unit
      # - run: pytest tests/contract
      - name: Everything
        run: pytest
      - run: echo "we do not use pytest-xdist here"
"""
    found = list(_pytest_invocations(workflow))
    assert [(source, arguments) for _, source, arguments in found] == [
        ("PYTEST_ADDOPTS", ["-m", "unit"]),
        ("pytest", ["tests/unit"]),
        ("pytest", []),
    ], found
    assert [bool(narrowing_arguments(arguments)) for _, _, arguments in found] == [
        True,
        True,
        False,
    ]


def test_a_step_that_merely_mentions_pytest_is_not_read_as_an_invocation() -> None:
    """Prose is not a command, however much it looks like one after `.index("pytest")`.

    The real CI step is named ``Test (pytest, offline suite only)``. Scanning every line
    containing the word read that label as ``pytest , offline suite only)``, whose bare
    words then looked exactly like path arguments — so the guard failed the merged trunk
    over a step's own title. A guard that fires on correct configuration is one the next
    person deletes, taking the real protection with it.
    """
    prose = """
jobs:
  test:
    steps:
      - name: Test (pytest, offline suite only)
        uses: actions/checkout@v4
      - run: echo "pytest is configured in pyproject.toml"
"""
    assert list(_pytest_invocations(prose)) == []
