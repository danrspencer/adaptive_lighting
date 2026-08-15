"""
Integration tests exercise the real Home Assistant event loop, template
engine, and automation/blueprint machinery via pytest-homeassistant-
custom-component - unlike tests/test_curve.py and tests/test_grouping.py
(bare-module imports, no HA dependency at all, see tests/conftest.py).

This is a separate, heavier suite for exactly that reason: it catches
what the pure-logic tests structurally cannot - bugs in how HA's own
trigger/condition/template engine behaves (e.g. `trigger` not being in
scope inside a template trigger's own value_template - the recovered
trigger's dead-on-arrival bug), and bugs in the HA-glue layer
(__init__.py's service registration, write_tracking.py's Store
persistence, context propagation).

Requires pytest-homeassistant-custom-component, which pins a specific
Home Assistant release and, transitively, the Python version that
release needs - this repo's whole floor (pyproject.toml's
requires-python) tracks that, rather than running a broader
lowest-common-denominator matrix for code that only ever runs inside a
real HA install anyway.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def hass_config_dir(tmp_path) -> str:
    """Overrides pytest-homeassistant-custom-component's own
    hass_config_dir fixture, which otherwise points hass.config.config_dir
    at the package's own bundled testing_config/ - a directory with no
    knowledge of this repo's integration at all, so
    async_setup_component(hass, "adaptive_lighting_helpers", {}) fails
    with "Integration not found" (confirmed live, not guessed - this is
    exactly the error before this fixture existed). Symlinks (not
    copies) custom_components/ into a throwaway tmp_path so HA reads the
    real, current source, while every other file HA writes during a test
    (.storage/, home-assistant.log, ...) lands in the tmp dir instead of
    polluting the actual repo. Also symlinks blueprints/ - the same
    "not found" problem applies to blueprint files: an automation's
    use_blueprint.path is resolved via hass.config.path("blueprints",
    "automation", ...), so tests/integration/test_blueprint.py's
    automations can only find the real adaptive_lighting.yaml if this
    tmp dir has it too."""
    (tmp_path / "custom_components").symlink_to(REPO_ROOT / "custom_components")
    (tmp_path / "blueprints").symlink_to(REPO_ROOT / "blueprints")
    return str(tmp_path)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Makes hass's component loader actually treat custom_components/
    (now findable via hass_config_dir above) as containing real,
    loadable integrations. Standard pytest-homeassistant-custom-component
    boilerplate (documented in its own README), autouse since every test
    in this directory needs it."""
    yield
