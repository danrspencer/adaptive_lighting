"""The manifest version is what HACS shows as installed, so it must not
drift from the changelog that explains it.

Checked here as well as in the release workflow because the workflow only
runs when a tag is pushed - by which point a mismatch means re-tagging.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "custom_components" / "flare" / "manifest.json"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def _version() -> str:
    return json.loads(MANIFEST.read_text())["version"]


def test_manifest_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _version()), "HACS compares versions, so they have to be orderable"


def test_the_changelog_describes_the_current_version():
    """A release whose version isn't in the changelog is one nobody can
    find out anything about - including which of it is breaking, which
    for this integration has mattered more than once."""
    assert f"## [{_version()}]" in CHANGELOG.read_text()


def test_the_release_workflow_checks_the_same_manifest_this_test_does():
    """The workflow greps a hardcoded path. If the component directory is
    ever renamed and only one of them is updated, releases start passing
    a check that reads a file that no longer exists."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert str(MANIFEST.relative_to(REPO_ROOT)) in workflow
