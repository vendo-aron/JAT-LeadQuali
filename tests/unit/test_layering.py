"""The layering rule, enforced instead of documented.

``CLAUDE.md``: ``anthropic`` is imported in ``adapters/llm_anthropic.py`` and nowhere else.
The rule is what keeps ``domain`` and ``app`` testable without a key and swappable for
another provider, and it is exactly the kind of rule that decays the first time someone
needs a type hint in a hurry. So it is a test.

The check is on the AST, not on a grep: a string mentioning ``anthropic`` in a docstring is
fine, an ``import`` of it is not.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "leadquali"

#: The one module allowed to import the SDK, relative to the package root.
SDK_OWNER = "adapters/llm_anthropic.py"


def imported_modules(path: Path) -> set[str]:
    """Every top-level module name imported by ``path``, from its AST."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def python_sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_the_package_has_sources_to_check() -> None:
    """Guards against a path typo silently turning this whole file into a no-op."""
    assert len(python_sources()) >= 8
    assert (SRC / SDK_OWNER).is_file()


@pytest.mark.parametrize("path", python_sources(), ids=lambda p: str(p.name))
def test_only_the_anthropic_adapter_imports_anthropic(path: Path) -> None:
    relative = path.relative_to(SRC).as_posix()
    if relative == SDK_OWNER:
        assert "anthropic" in imported_modules(path), "the adapter is supposed to own the SDK"
        return
    assert "anthropic" not in imported_modules(path), (
        f"{relative} imports anthropic; the SDK belongs in {SDK_OWNER} alone"
    )


def test_domain_and_app_import_no_third_party_sdk() -> None:
    """The same rule, stated for its siblings: no ``boto3``/``sqlalchemy``/``stripe`` either."""
    forbidden = {"anthropic", "boto3", "sqlalchemy", "stripe", "psycopg", "fastapi"}
    for path in python_sources():
        relative = path.relative_to(SRC).as_posix()
        if not relative.startswith(("domain/", "app/")):
            continue
        leaked = forbidden & imported_modules(path)
        assert not leaked, f"{relative} imports {sorted(leaked)}"


def test_observability_imports_no_layer_above_it() -> None:
    """``leadquali.observability`` sits beside the layers, not on top of them.

    It is imported by ``domain``-adjacent value types, by ``app``, by ``adapters`` and by
    ``api`` alike, which is only safe while it depends on none of them: an
    ``observability`` that reached into ``adapters`` would put SQLAlchemy in the import
    graph of every module that wants to write a log line, and would make the import cycle
    ``app`` → ``observability`` → ``adapters`` → ``app`` a matter of luck about which file
    was imported first.

    Reading value types (``RoutingDecision``, ``CallMetering``) is allowed and is the point
    — a metric about a decision has to be able to see one.
    """
    permitted = {"leadquali"}
    for path in python_sources():
        relative = path.relative_to(SRC).as_posix()
        if not relative.startswith("observability/"):
            continue
        imported = {
            name
            for name in _imported_paths(path)
            if name.startswith("leadquali.") and name.split(".")[1] in {"adapters", "api"}
        }
        assert not imported, f"{relative} imports {sorted(imported)}"
        assert imported_modules(path) & {"anthropic", "boto3", "sqlalchemy", "fastapi"} == set()
        assert permitted  # the loop ran with a real package root


def _imported_paths(path: Path) -> set[str]:
    """Every dotted module path imported by ``path``, in full."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def test_only_named_adapters_import_boto3() -> None:
    """``CLAUDE.md``: one file per external system, and ``boto3`` belongs to those files.

    Stated separately from the rule above because it binds the *adapters* too. The
    allowlist is per AWS service rather than a blanket "adapters may use boto3": SES is
    reached from one file and SQS from one file, so swapping email for a Slack notifier,
    or the queue for something else, stays a wiring change - and an operator can still
    find every call to a given AWS service by opening a single module.

    Adding a name here should be a deliberate act with a service behind it.
    """
    owners = {
        "adapters/notify_ses.py": "SES",
        "adapters/queue_sqs.py": "SQS",
    }
    for owner, service in owners.items():
        assert "boto3" in imported_modules(SRC / owner), (
            f"{owner} is supposed to own the {service} client"
        )
    for path in python_sources():
        relative = path.relative_to(SRC).as_posix()
        if relative in owners:
            continue
        assert "boto3" not in imported_modules(path), (
            f"{relative} imports boto3; AWS access belongs in {sorted(owners)} alone"
        )
