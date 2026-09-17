"""The templates are packaged, render, and never leak into a log or an error page.

A template that is not declared in ``[tool.setuptools.package-data]`` is a 500 in Lambda
and a pass in the tests, which is the worst combination available: the source tree has the
file, the wheel does not, and nothing notices until a staff member opens the page in
production. So the globs are checked against what is actually on disk.

The rest of the file is invariant 5 at the admin's two riskiest edges. The lead detail page
renders a submitter's contact details on purpose; an error page and a log line must not.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from leadquali.api.admin import _failure_page, _templates
from leadquali.observability import (
    EVENT_ADMIN_CONFIG_CHANGED,
    EVENT_ADMIN_CSRF_REJECTED,
    EVENT_ADMIN_LEAD_PROMOTED,
    EVENT_ADMIN_LOGIN_FAILED,
    EVENT_ADMIN_PAGE_FAILED,
    EVENT_ADMIN_RERUN_COMPLETED,
    EVENT_ADMIN_SESSION_REJECTED,
    log_admin_config_changed,
    log_admin_lead_promoted,
    log_admin_login_failed,
    log_admin_rerun_completed,
)
from tests.logcapture import capture_json_logs

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "src" / "leadquali" / "api" / "templates"

#: Strings that must never appear in a log line or on an error page. Drawn from a lead a
#: rep might actually have submitted, because the failure being defended against is an
#: exception raised while rendering one.
A_LEAD = {
    "full_name": "Priya Raghunathan",
    "email": "priya.raghunathan@northstar-logistics.example",
    "phone": "+44 20 7946 0958",
    "message": "We are replacing a spreadsheet this quarter and have budget signed off.",
}


def package_data_globs() -> list[str]:
    """The declared globs, read from ``pyproject.toml`` rather than restated here."""
    parsed = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    globs: list[str] = parsed["tool"]["setuptools"]["package-data"]["leadquali"]
    return globs


def template_files() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


# ------------------------------------------------------------------------- packaging


def test_there_are_templates_to_check() -> None:
    """Guards against a path typo turning this whole file into a no-op."""
    assert len(template_files()) >= 10


@pytest.mark.parametrize("path", template_files(), ids=lambda p: p.name)
def test_every_template_is_declared_as_package_data(path: Path) -> None:
    """Otherwise it is in the repository, absent from the wheel, and a 500 in Lambda."""
    relative = path.relative_to(REPO / "src" / "leadquali").as_posix()

    assert any(fnmatch.fnmatch(relative, glob) for glob in package_data_globs()), (
        f"{relative} matches none of {package_data_globs()}; add a glob to "
        "[tool.setuptools.package-data] or the template will not ship"
    )


@pytest.mark.parametrize("path", template_files(), ids=lambda p: p.name)
def test_every_template_compiles(path: Path) -> None:
    """A syntax error in a template is a runtime error on one page and nowhere else."""
    name = path.relative_to(TEMPLATES).as_posix()

    assert _templates().get_template(name) is not None


def test_the_loader_finds_every_file_on_disk() -> None:
    """``PackageLoader`` reads from the installed package; the two views must agree."""
    on_disk = {path.relative_to(TEMPLATES).as_posix() for path in template_files()}

    assert set(_templates().list_templates()) == on_disk


def test_no_template_loads_anything_over_the_network() -> None:
    """§9: no CSS framework from a CDN, no font, no script, no build step.

    Checked on the files rather than on the response headers, because the
    Content-Security-Policy only *blocks* such a reference — this is what stops one being
    written in the first place, where the failure would be a page that looks broken.
    """
    for path in template_files():
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text, f"{path.name} references an external resource"
        assert "https://" not in text, f"{path.name} references an external resource"
        assert "<script" not in text.lower(), f"{path.name} carries a script tag"


# ------------------------------------------------------------------- the error page


def test_the_error_page_carries_only_its_own_sentence() -> None:
    """It is rendered while something has gone wrong *around a lead*, so it may not echo
    the request, the row, or the exception."""
    body = bytes(_failure_page("something went wrong rendering that page").body).decode("utf-8")

    assert "something went wrong rendering that page" in body
    for value in A_LEAD.values():
        assert value not in body


def test_the_error_page_is_not_cached() -> None:
    response = _failure_page("nope")

    assert "no-store" in response.headers["cache-control"]
    assert response.status_code == 500


# --------------------------------------------------------- invariant 5 on admin events


def test_the_admin_events_are_named_and_distinct() -> None:
    names = {
        EVENT_ADMIN_LOGIN_FAILED,
        EVENT_ADMIN_CONFIG_CHANGED,
        EVENT_ADMIN_RERUN_COMPLETED,
        EVENT_ADMIN_LEAD_PROMOTED,
        EVENT_ADMIN_SESSION_REJECTED,
        EVENT_ADMIN_CSRF_REJECTED,
        EVENT_ADMIN_PAGE_FAILED,
    }

    assert len(names) == 7
    assert all(name.startswith("admin.") for name in names)


def emit_every_admin_event() -> list[dict[str, Any]]:
    """Emit one of each admin event with a lead's data in every field that takes text."""
    logger = logging.getLogger("leadquali.test.admin")
    with capture_json_logs() as logs:
        # The username is a staff handle, but a staff member who typed their email address
        # into the username box is exactly the case this has to survive.
        log_admin_login_failed(logger, username=A_LEAD["email"], gated=True)
        log_admin_config_changed(
            logger, tenant_id="acme", changed_by="ada", version=2, fields_changed=3
        )
        log_admin_rerun_completed(
            logger,
            tenant_id="acme",
            leads=25,
            tier_changes=4,
            cost_usd=Decimal("0.45"),
        )
        log_admin_lead_promoted(
            logger,
            tenant_id="acme",
            lead_id="0f3c9a12",
            case_id="real_acme_0f3c9a12",
            promoted_by="ada",
        )
        records = logs.records()
        blob = logs.text
    assert blob
    # Even the address typed into a username box is caught by the formatter's net, which
    # is the defence-in-depth half of invariant 5 doing its job.
    assert A_LEAD["email"] not in blob
    return records


def test_no_admin_event_carries_a_lead_s_data() -> None:
    records = emit_every_admin_event()

    assert len(records) == 4
    for record in records:
        rendered = json.dumps(record)
        for field in ("full_name", "phone", "message"):
            assert A_LEAD[field] not in rendered, f"{record.get('event')} carries {field}"


def test_the_config_change_event_records_the_change_without_the_values() -> None:
    """A routing rule holds a sales inbox and the ICP prose is a customer's positioning.
    The document belongs in ``tenant_config_versions``, which has access control; a log
    aggregator does not."""
    record = next(
        found
        for found in emit_every_admin_event()
        if found.get("event") == EVENT_ADMIN_CONFIG_CHANGED
    )

    assert record["tenant_id"] == "acme"
    assert record["changed_by"] == "ada"
    assert record["config_version"] == 2
    assert record["fields_changed"] == 3
    assert "config" not in record
    assert "icp_description" not in json.dumps(record)


def test_the_rerun_event_says_the_spend_is_not_billable() -> None:
    """#33 cannot express "spent on this tenant, not billable to them" — this line is the
    only record of what a rubric experiment cost."""
    record = next(
        found
        for found in emit_every_admin_event()
        if found.get("event") == EVENT_ADMIN_RERUN_COMPLETED
    )

    assert record["billable"] is False
    assert Decimal(str(record["cost_usd"])) == Decimal("0.45")
    assert record["leads"] == 25
