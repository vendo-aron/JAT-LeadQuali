"""``LeadForm`` moved to ``app`` — one class, two spellings, nothing else changed.

#31's review added the rule that ``adapters`` may not import ``leadquali.api``, and #36
broke it: the re-run reads ``leads.raw_payload`` back out of Postgres and has to parse it
the way ingest parsed it on the way in. The fix follows the precedent ``app/credentials.py``
set — move the shared thing down a layer rather than allowlist the violation — and this
file pins the two properties that make the fix a fix rather than a duplication:

* ``api.schemas.LeadForm`` and ``app.lead_payload.LeadForm`` are the **same class object**,
  so there is one description of what a stored payload looks like. Two would drift inside a
  release, and the copy that drifts is the one nobody tests against.
* ``api/schemas.py``'s public surface is **unchanged**, so #17's ingest path, its tests and
  #22's golden-set loader are untouched by the move.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from leadquali.api import schemas
from leadquali.app import lead_payload
from leadquali.app.lead_payload import (
    MAX_EXTRA_FIELDS,
    MAX_MESSAGE_CHARS,
    MAX_SHORT_FIELD_CHARS,
    LeadForm,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "leadquali"

#: What ``api/schemas.py`` exported before the move. Restated as a literal rather than read
#: from the module, because the whole point is that it did not change — comparing the module
#: against itself would pass however much it changed.
SCHEMAS_PUBLIC_SURFACE_BEFORE_THE_MOVE = {
    "MAX_BODY_BYTES",
    "MAX_EXTRA_FIELDS",
    "MAX_MESSAGE_CHARS",
    "MAX_SHORT_FIELD_CHARS",
    "SOURCE_PATTERN",
    "SUBMISSION_ID_PATTERN",
    "ErrorResponse",
    "FieldError",
    "IngestAccepted",
    "LeadForm",
    "LeadIngestRequest",
    "ValidationErrorResponse",
}


# ----------------------------------------------------------------- one class, not two


def test_both_spellings_name_the_same_class() -> None:
    """A copy would be a second answer to "what does a stored payload look like"."""
    assert schemas.LeadForm is lead_payload.LeadForm


@pytest.mark.parametrize("name", ["MAX_SHORT_FIELD_CHARS", "MAX_MESSAGE_CHARS", "MAX_EXTRA_FIELDS"])
def test_the_caps_that_travelled_with_it_are_the_same_objects(name: str) -> None:
    assert getattr(schemas, name) is getattr(lead_payload, name)


def test_the_schema_module_no_longer_declares_the_form() -> None:
    """Asserted on the AST, so re-declaring the class alongside the import fails here.

    Two classes both named ``LeadForm``, one shadowing the other, is exactly the state this
    move exists to prevent and exactly the state an identity check alone would still pass
    if the import came second.
    """
    tree = ast.parse((SRC / "api" / "schemas.py").read_text(encoding="utf-8"))
    declared = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}

    assert "LeadForm" not in declared
    assert "LeadIngestRequest" in declared, "the envelope stays in the api layer"


# ------------------------------------------------------- the public surface is unchanged


def test_the_schema_module_exports_exactly_what_it_did_before() -> None:
    assert set(schemas.__all__) == SCHEMAS_PUBLIC_SURFACE_BEFORE_THE_MOVE


@pytest.mark.parametrize("name", sorted(SCHEMAS_PUBLIC_SURFACE_BEFORE_THE_MOVE))
def test_every_previously_exported_name_is_still_importable(name: str) -> None:
    """``__all__`` is a list of strings; a name in it that does not resolve is a runtime
    error for anybody doing ``from leadquali.api.schemas import *``."""
    assert getattr(schemas, name, None) is not None


def test_the_envelope_still_composes_the_form() -> None:
    """The seam between the two modules, exercised rather than asserted structurally."""
    request = schemas.LeadIngestRequest.model_validate(
        {"submission_id": "a" * 12, "form": {"email": "ada@example.com", "utm": "linkedin"}}
    )

    assert isinstance(request.form, lead_payload.LeadForm)
    assert request.form.to_submission().extra == {"utm": "linkedin"}


# ------------------------------------------------ the behaviour itself, unchanged by the move


def test_known_fields_are_typed_and_unknown_ones_are_kept() -> None:
    form = LeadForm.model_validate(
        {"full_name": "Ada Lovelace", "email": "ada@example.com", "utm_source": "linkedin"}
    )

    submission = form.to_submission()

    assert submission.full_name == "Ada Lovelace"
    assert submission.extra == {"utm_source": "linkedin"}


def test_a_nested_document_in_a_form_field_is_refused() -> None:
    """A form does not produce one, and accepting it would mean stringifying arbitrarily
    deep structure on the request path."""
    with pytest.raises(ValidationError):
        LeadForm.model_validate({"attachments": [{"name": "x"}]})


def test_the_extra_field_count_is_capped() -> None:
    payload: dict[str, Any] = {f"f{index:03d}": "v" for index in range(MAX_EXTRA_FIELDS + 10)}

    submission = LeadForm.model_validate(payload).to_submission()

    assert len(submission.extra) == MAX_EXTRA_FIELDS


def test_an_over_long_message_is_refused_rather_than_truncated() -> None:
    """This layer decides what may be *stored*; #12's renderer decides what the model is
    shown, and that one truncates. The two must not be confused."""
    with pytest.raises(ValidationError):
        LeadForm.model_validate({"message": "a" * (MAX_MESSAGE_CHARS + 1)})


def test_the_schema_caps_are_far_above_the_renderer_s() -> None:
    """A long genuine enquiry must not be answered with a 422."""
    from leadquali.prompts import lead as renderer

    assert MAX_MESSAGE_CHARS > renderer.MAX_MESSAGE_CHARS
    assert MAX_SHORT_FIELD_CHARS > renderer.MAX_SHORT_FIELD_CHARS
    assert MAX_EXTRA_FIELDS > renderer.MAX_EXTRA_FIELDS


# ----------------------------------------------------------------- the layering itself


def test_the_payload_module_reaches_no_layer_above_it() -> None:
    """It is imported by ``api`` *and* by ``adapters``, which is only safe while it depends
    on neither. The general rule is in ``tests/unit/test_layering.py``; this states it for
    the module the rule was bent around."""
    tree = ast.parse((SRC / "app" / "lead_payload.py").read_text(encoding="utf-8"))
    imported = {node.module for node in tree.body if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }

    assert not {name for name in imported if name and name.startswith("leadquali.api")}
    assert not {name for name in imported if name and name.startswith("leadquali.adapters")}
