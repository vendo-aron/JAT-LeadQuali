"""One CloudFormation loader, shared by the infrastructure tests.

#26 introduced the loader that tolerates CloudFormation's `!Ref`/`!GetAtt` short forms;
#27 added a second template to assert against. It lives here rather than in either test
module so that there is exactly one of it: two loaders that drift is how a test starts
passing against a shape the real template no longer has.

Nothing here resolves intrinsic functions. A `!Ref` comes back as `{"Fn::Ref": "Name"}`,
which is what the tests want — asserting that a property *references* a parameter is a
stronger statement than asserting on whatever that parameter happens to default to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

INFRA_DIR = Path(__file__).resolve().parents[2] / "infra"

#: The application stack: API Gateway, ingest, queue, worker, migrations (#26, #27).
APPLICATION_TEMPLATE_PATH = INFRA_DIR / "template.yaml"

#: The network and data stack: VPC, egress, RDS Postgres, RDS Proxy (#27).
NETWORK_TEMPLATE_PATH = INFRA_DIR / "network.yaml"


class CloudFormationLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation's `!Ref`/`!Sub`/`!GetAtt` short forms."""


def _tag(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> dict[str, Any]:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:  # pragma: no cover - the three node kinds above are exhaustive in PyYAML
        raise TypeError(f"unexpected node {type(node).__name__} for tag !{tag_suffix}")
    return {f"Fn::{tag_suffix}": value}


CloudFormationLoader.add_multi_constructor("!", _tag)


def load_template(path: Path) -> dict[str, Any]:
    """Parse a CloudFormation template into plain dicts.

    Args:
        path: The template to read.

    Returns:
        The template as nested built-in types, with intrinsic function tags turned into
        single-key dicts such as ``{"Fn::Ref": "Stage"}``.
    """
    # S506 is about untrusted YAML instantiating arbitrary objects. `CloudFormationLoader`
    # derives from `SafeLoader` and only adds constructors that turn CloudFormation's
    # `!Ref`-style tags into plain dicts, and the input is a file from this repository.
    loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=CloudFormationLoader)  # noqa: S506
    assert isinstance(loaded, dict)
    return loaded


def resources(template: dict[str, Any]) -> dict[str, Any]:
    """Return a template's ``Resources`` block.

    Args:
        template: A template loaded by :func:`load_template`.

    Returns:
        The resource logical-id to definition mapping.
    """
    block = template["Resources"]
    assert isinstance(block, dict)
    return block


def parameters(template: dict[str, Any]) -> dict[str, Any]:
    """Return a template's ``Parameters`` block.

    Args:
        template: A template loaded by :func:`load_template`.

    Returns:
        The parameter name to specification mapping.
    """
    block = template["Parameters"]
    assert isinstance(block, dict)
    return block
