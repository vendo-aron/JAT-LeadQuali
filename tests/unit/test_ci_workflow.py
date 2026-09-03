"""The CI workflow is configuration, so its invariants are asserted, not assumed.

A workflow file cannot be executed here (and is only ever executed by GitHub), so the
guarantees that matter are pinned as parsed-YAML assertions: the interpreter version, the
presence of all four quality gates as separate steps, and — most importantly — that the
default test run can never reach a billable Anthropic call or an external service.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The workflow is asserted at whichever of the two paths holds it. `.github/workflows/`
# is where GitHub runs it from and is the intended home; `ci/` is the staging path used
# while the authoring credential lacks GitHub's `workflow` scope (see the file's header).
# Accepting both means activating CI is a `git mv` and nothing else - no test edit, so no
# chance of the move silently disabling these assertions.
_CANDIDATE_PATHS = (
    _REPO_ROOT / ".github" / "workflows" / "ci.yml",
    _REPO_ROOT / "ci" / "github-actions-ci.yml",
)


def _workflow_path() -> Path:
    """Return the CI workflow, wherever it currently lives."""
    for candidate in _CANDIDATE_PATHS:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(path) for path in _CANDIDATE_PATHS)
    raise AssertionError(f"no CI workflow found; looked in: {searched}")


WORKFLOW_PATH = _workflow_path()

# A YAML mapping's keys are not all strings here - see `_triggers` for the `on` quirk -
# so the root is keyed by `object`. Values are `Any` because a workflow is an untyped
# document; every value is narrowed with `isinstance` before it is asserted on.
Workflow = dict[object, Any]


def _load_workflow() -> Workflow:
    """Parse the CI workflow into a mapping."""
    with WORKFLOW_PATH.open(encoding="utf-8") as handle:
        loaded: object = yaml.safe_load(handle)
    assert isinstance(loaded, dict), "the workflow must parse to a mapping"
    return loaded


def _triggers(workflow: Workflow) -> dict[str, Any]:
    """Return the `on:` block.

    YAML 1.1 resolves the bare key `on` to the boolean ``True``, which is what PyYAML
    does and what GitHub's own parser tolerates; accept either spelling.
    """
    raw: object = workflow.get("on", workflow.get(True))
    assert isinstance(raw, dict), "`on:` must be a mapping of trigger names"
    return raw


@pytest.fixture(scope="module")
def workflow() -> Workflow:
    return _load_workflow()


@pytest.fixture(scope="module")
def build_job(workflow: Workflow) -> dict[str, Any]:
    jobs: object = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert len(jobs) == 1, "one job keeps the required status check stable"
    job: object = next(iter(jobs.values()))
    assert isinstance(job, dict)
    return job


@pytest.fixture(scope="module")
def steps(build_job: dict[str, Any]) -> list[dict[str, Any]]:
    raw: object = build_job["steps"]
    assert isinstance(raw, list)
    return [step for step in raw if isinstance(step, dict)]


def _run_commands(steps: list[dict[str, Any]]) -> list[str]:
    return [str(step["run"]) for step in steps if "run" in step]


def test_workflow_file_exists_and_parses() -> None:
    assert WORKFLOW_PATH.is_file(), f"missing workflow at {WORKFLOW_PATH}"
    assert _load_workflow()


def test_runs_on_push_and_pull_request(workflow: Workflow) -> None:
    triggers = _triggers(workflow)
    assert {"push", "pull_request"} <= set(triggers)


def test_triggers_are_not_filtered_to_a_branch(workflow: Workflow) -> None:
    """The repository has no `main` yet; a branch filter would silently disable CI."""
    for name, config in _triggers(workflow).items():
        if config is None:
            continue
        assert isinstance(config, dict), f"unexpected shape for trigger {name!r}"
        assert "branches" not in config, f"trigger {name!r} must not filter by branch"
        assert "branches-ignore" not in config


def test_workflow_permissions_are_read_only(workflow: Workflow) -> None:
    assert workflow["permissions"] == {"contents": "read"}


def test_superseded_runs_on_the_same_ref_are_cancelled(workflow: Workflow) -> None:
    concurrency: object = workflow["concurrency"]
    assert isinstance(concurrency, dict)
    assert concurrency["cancel-in-progress"] is True
    assert "github.ref" in str(concurrency["group"])


def test_job_is_bounded_and_runs_on_ubuntu(build_job: dict[str, Any]) -> None:
    assert build_job["runs-on"] == "ubuntu-latest"
    timeout: object = build_job["timeout-minutes"]
    assert isinstance(timeout, int)
    assert 0 < timeout <= 15, "a lint+type+unit-test job that takes longer is stuck"


def test_python_is_313_with_pip_cache_keyed_on_pyproject(steps: list[dict[str, Any]]) -> None:
    setup = [step for step in steps if str(step.get("uses", "")).startswith("actions/setup-python")]
    assert len(setup) == 1, "exactly one Python must be set up"
    params: object = setup[0]["with"]
    assert isinstance(params, dict)
    assert str(params["python-version"]) == "3.13"
    assert params["cache"] == "pip"
    assert "pyproject.toml" in str(params["cache-dependency-path"])


def test_dependencies_are_installed_from_the_dev_extra(steps: list[dict[str, Any]]) -> None:
    assert any('pip install -e ".[dev]"' in command for command in _run_commands(steps))


@pytest.mark.parametrize(
    "check",
    ["ruff check .", "ruff format --check .", "mypy", "pytest"],
)
def test_each_quality_gate_is_its_own_named_step(check: str, steps: list[dict[str, Any]]) -> None:
    """One command per step, so the failing gate is legible from the run summary."""
    matching = [
        step for step in steps if "run" in step and str(step["run"]).strip().startswith(check)
    ]
    assert len(matching) == 1, f"expected exactly one step running {check!r}"
    assert matching[0].get("name"), f"the {check!r} step must be named"


def test_pytest_cannot_run_billable_or_external_tests(steps: list[dict[str, Any]]) -> None:
    """The default job must never spend money or reach the network."""
    pytest_commands = [c for c in _run_commands(steps) if c.strip().startswith("pytest")]
    assert len(pytest_commands) == 1
    command = pytest_commands[0]
    assert '-m "not live_api and not integration"' in command
    assert "tests/evals" not in command


def test_no_credentials_are_exposed_to_the_job() -> None:
    """No API keys reach the job - not as `env`, not as a `secrets.*` expression.

    Comment lines are skipped: the workflow is expected to *mention* the keys it
    deliberately withholds.
    """
    directives = "\n".join(
        line
        for line in WORKFLOW_PATH.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    for forbidden in ("ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        assert forbidden not in directives
    assert "secrets." not in directives
