"""Deterministic hash over file contents + env + free-form text.

Suitable as the `Trigger` property on a CDK Custom Resource: when
any input changes, the hash changes, CFN sees the property diff,
and the CR's underlying Lambda re-runs. Replaces hand-incremented
`Trigger: "v1" -> "v2" -> ...` literals where the operator had to
remember to bump the constant whenever they touched something
upstream.

Extracted from authentik_stack._stamp_blueprints so any other stack
that wires a CR can share the same hash recipe.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from collections.abc import Iterable, Mapping


def hash_inputs(
    *,
    files: Iterable[pathlib.Path] = (),
    env: Mapping[str, str] | None = None,
    extra: str = "",
) -> str:
    """16-char SHA256 over (basename, contents) of `files` +
    alphabetized `env` items + the `extra` opaque string.

    Names are basenames (not full paths) so a refactor that moves a
    file without changing its content doesn't churn the hash.
    """
    file_pairs = sorted((p.name, p.read_text()) for p in files)
    payload = json.dumps(
        {
            "files": file_pairs,
            "env": dict(sorted((env or {}).items())),
            "extra": extra,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def expand_globs(root: pathlib.Path, *patterns: str) -> list[pathlib.Path]:
    """Resolve `pathlib.Path.glob` patterns rooted at `root` into a
    sorted, deduplicated list of regular files.
    """
    found: set[pathlib.Path] = set()
    for pattern in patterns:
        for p in root.glob(pattern):
            if p.is_file():
                found.add(p)
    return sorted(found)
