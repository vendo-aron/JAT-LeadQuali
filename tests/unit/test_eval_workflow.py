"""The eval workflow is configuration that spends money, so its invariants are asserted.

A workflow file is only ever executed by GitHub, so what can be checked here is the
policy encoded in it — and the policy is the whole point. One mistake in this file (a
``push`` trigger, a key handed to the wrong step) is a bill and a rate-limit incident
rather than a red build, and neither is visible until it has already happened.

The assertions follow ``tests/unit/test_ci_workflow.py``, including its two-path
resolution: ``.github/workflows/`` is where GitHub runs it from, ``ci/`` is the staging
path used while the authoring credential lacks GitHub's ``workflow`` scope, and accepting
both means activating the workflow is a ``git mv`` with no test edit — so the move cannot
silently disable what is asserted below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

_CANDIDATE_PATHS = (
    _REPO_ROOT / ".github" / "workflows" / "eval.yml",
    _REPO_ROOT / "ci" / "github-actions-eval.yml",
)


def _workflow_path() -> Path:
    """Return the eval workflow, wherever it currently lives."""
    for candidate in _CANDIDATE_PATHS:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(path) for path in _CANDIDATE_PATHS)
    raise AssertionError(f"no eval workflow found; looked in: {searched}")


WORKFLOW_PATH = _workflow_path()

# Keyed by `object` because of the `on` quirk below; values are `Any` because a workflow is
# an untyped document. Every value is narrowed with `isinstance` before it is asserted on.
Workflow = dict[object, Any]


def _load_workflow() -> Workflow:
    with WORKFLOW_PATH.open(encoding="utf-8") as handle:
        loaded: object = yaml.safe_load(handle)
    assert isinstance(loaded, dict), "the workflow must parse to a mapping"
    return loaded


def _triggers(workflow: Workflow) -> dict[str, Any]:
    """Return the ``on:`` block.

    YAML 1.1 resolves the bare key ``on`` to the boolean ``True``, which is what PyYAML
    does and what GitHub's own parser tolerates; accept either spelling.
    """
    raw: object = workflow.get("on", workflow.get(True))
    assert isinstance(raw, dict), "`on:` must be a mapping of trigger names"
    return raw


@pytest.fixture(scope="module")
def workflow() -> Workflow:
    return _load_workflow()


@pytest.fixture(scope="module")
def eval_job(workflow: Workflow) -> dict[str, Any]:
    jobs: object = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert len(jobs) == 1, "one job keeps the summary and the artifact in one place"
    job: object = next(iter(jobs.values()))
    assert isinstance(job, dict)
    return job


@pytest.fixture(scope="module")
def steps(eval_job: dict[str, Any]) -> list[dict[str, Any]]:
    raw: object = eval_job["steps"]
    assert isinstance(raw, list)
    return [step for step in raw if isinstance(step, dict)]


def _run_commands(steps: list[dict[str, Any]]) -> list[str]:
    return [str(step["run"]) for step in steps if "run" in step]


def _eval_step(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """The one step that actually spends money."""
    matching = [step for step in steps if "run" in step and "--confirm-spend" in str(step["run"])]
    assert len(matching) == 1, "exactly one step may run the paid eval"
    return matching[0]


def test_workflow_file_exists_and_parses() -> None:
    assert WORKFLOW_PATH.is_file(), f"missing workflow at {WORKFLOW_PATH}"
    assert _load_workflow()


def test_the_header_says_how_to_activate_it() -> None:
    """Staged at ``ci/`` is only useful if the move is written down where it is found."""
    header = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "git mv ci/github-actions-eval.yml .github/workflows/eval.yml" in header
    assert "ANTHROPIC_API_KEY" in header, "the secret it needs must be named in the header"


def test_it_runs_only_on_manual_dispatch(workflow: Workflow) -> None:
    """The trigger policy is the cost control. Anything automatic bills every commit."""
    assert set(_triggers(workflow)) == {"workflow_dispatch"}


@pytest.mark.parametrize("forbidden", ["push", "pull_request", "pull_request_target", "schedule"])
def test_no_automatic_trigger_can_fire_a_paid_run(workflow: Workflow, forbidden: str) -> None:
    assert forbidden not in _triggers(workflow)


def test_effort_and_prompt_version_are_dispatch_inputs(workflow: Workflow) -> None:
    dispatch: object = _triggers(workflow)["workflow_dispatch"]
    assert isinstance(dispatch, dict)
    inputs: object = dispatch["inputs"]
    assert isinstance(inputs, dict)
    assert {"effort", "prompt_version"} <= set(inputs)
    effort: object = inputs["effort"]
    assert isinstance(effort, dict)
    assert effort["type"] == "choice"
    # The five levels claude-opus-5 accepts, matching EFFORT_LEVELS. A sixth here would be
    # a dispatch that fails after the checkout rather than in the form.
    assert set(effort["options"]) == {"low", "medium", "high", "xhigh", "max"}
    assert effort["default"] == "medium"


def test_the_prompt_version_input_is_verified_before_any_spend(
    steps: list[dict[str, Any]],
) -> None:
    """A number filed against the wrong prompt version is worse than no number."""
    commands = _run_commands(steps)
    verification = [command for command in commands if "PROMPT_VERSION" in command]
    assert len(verification) == 1
    assert "inputs.prompt_version" in verification[0]
    assert commands.index(verification[0]) < commands.index(_eval_step(steps)["run"])


def test_the_estimate_runs_before_the_paid_run(steps: list[dict[str, Any]]) -> None:
    commands = _run_commands(steps)
    estimates = [command for command in commands if "--estimate" in command]
    assert len(estimates) == 1
    assert commands.index(estimates[0]) < commands.index(_eval_step(steps)["run"])


def test_workflow_permissions_are_read_only(workflow: Workflow) -> None:
    assert workflow["permissions"] == {"contents": "read"}


def test_a_superseded_run_is_not_cancelled(workflow: Workflow) -> None:
    """Unlike CI, a superseded run here has already spent money. Queue it, do not bin it."""
    concurrency: object = workflow["concurrency"]
    assert isinstance(concurrency, dict)
    assert concurrency["cancel-in-progress"] is False


def test_the_job_is_bounded_and_runs_on_ubuntu(eval_job: dict[str, Any]) -> None:
    assert eval_job["runs-on"] == "ubuntu-latest"
    timeout: object = eval_job["timeout-minutes"]
    assert isinstance(timeout, int)
    assert 0 < timeout <= 60, "a run that takes longer than an hour is a hung model call"


def test_python_is_313_with_the_dev_extras(steps: list[dict[str, Any]]) -> None:
    setup = [step for step in steps if str(step.get("uses", "")).startswith("actions/setup-python")]
    assert len(setup) == 1
    params: object = setup[0]["with"]
    assert isinstance(params, dict)
    assert str(params["python-version"]) == "3.13"
    assert any('pip install -e ".[dev]"' in command for command in _run_commands(steps))


def test_the_key_reaches_exactly_one_step_and_it_is_the_eval(
    steps: list[dict[str, Any]],
) -> None:
    """A key on the job, or on the checkout, is a key every future step inherits."""
    with_key = [step for step in steps if "ANTHROPIC_API_KEY" in str(step.get("env", {}))]
    assert len(with_key) == 1
    assert with_key[0] is _eval_step(steps)
    assert with_key[0]["env"]["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"


def test_the_key_is_not_exposed_to_the_job_or_the_workflow(
    workflow: Workflow, eval_job: dict[str, Any]
) -> None:
    assert "env" not in workflow, "a workflow-level env is inherited by every step"
    assert "env" not in eval_job, "a job-level env is inherited by every step"


def test_the_paid_run_passes_the_confirmation_flag_and_the_dispatch_inputs(
    steps: list[dict[str, Any]],
) -> None:
    command = str(_eval_step(steps)["run"])
    assert "python -m tests.evals.run_eval" in command
    assert "--confirm-spend" in command
    for name in ("effort", "repeat", "concurrency"):
        assert f"inputs.{name}" in command, f"the {name} input must reach the harness"


def test_the_report_is_posted_to_the_run_summary_even_when_the_run_fails(
    steps: list[dict[str, Any]],
) -> None:
    """A run that ends on an injection finding is precisely the one somebody must read."""
    summary = [step for step in steps if "GITHUB_STEP_SUMMARY" in str(step.get("run", ""))]
    assert len(summary) == 1
    assert summary[0]["if"] == "always()"
    assert "self-consistency" in str(summary[0]["run"]), (
        "the summary must carry the synthetic-set caveat: it is the part people screenshot"
    )


def test_the_json_result_is_uploaded_for_later_comparison(steps: list[dict[str, Any]]) -> None:
    uploads = [step for step in steps if str(step.get("uses", "")).startswith("actions/upload-")]
    assert len(uploads) == 1
    assert uploads[0]["if"] == "always()"
    params: object = uploads[0]["with"]
    assert isinstance(params, dict)
    assert "eval-results/" in str(params["path"])


def test_every_step_is_named(steps: list[dict[str, Any]]) -> None:
    """The Actions log is the only view most people get of a run that cost money."""
    for step in steps:
        assert step.get("name"), f"unnamed step: {step}"
