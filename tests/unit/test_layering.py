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


def test_only_the_ses_adapter_imports_boto3() -> None:
    """``CLAUDE.md``: one file per external system, and ``boto3`` belongs to that one.

    Stated separately from the rule above because it binds the *adapters* too: SES is
    reached from ``adapters/notify_ses.py`` and nowhere else, so swapping email for a Slack
    notifier is a wiring change and an operator can find every AWS call by opening one file.
    """
    owner = "adapters/notify_ses.py"
    assert "boto3" in imported_modules(SRC / owner), "the adapter is supposed to own the SDK"
    for path in python_sources():
        relative = path.relative_to(SRC).as_posix()
        if relative == owner:
            continue
        assert "boto3" not in imported_modules(path), (
            f"{relative} imports boto3; AWS belongs in {owner} alone"
        )
