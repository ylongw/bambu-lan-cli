# SPDX-License-Identifier: AGPL-3.0-or-later
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "audit", Path(__file__).parents[1] / "scripts/privacy_audit.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_exact_match_and_static_scan():
    assert audit.content_hits(b"unit-secret", {b"unit-secret"}) == 1
    assert audit.content_hits(b"generic clean fixture", set()) == 0
    # Construct forbidden path at runtime; no real local identity in fixtures.
    assert (
        audit.content_hits(("/" + "Users" + "/" + "example-user").encode(), set()) == 1
    )


def test_forbidden_names():
    for name in (
        "config.yaml",
        "config.local.yaml",
        ".env",
        ".env.test",
        "private.env",
        "private.bak-old",
        "run.log",
        "x.local.yml",
    ):
        assert audit.forbidden_name(name)
    for name in ("config.example.yaml", "README.md", "src/module.py"):
        assert not audit.forbidden_name(name)
