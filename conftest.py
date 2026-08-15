"""
pytest_plugins must be declared in the actual rootdir conftest.py, not
a nested one (pytest 9+ hard error, not just a deprecation warning) -
this is the only reason this file exists at the repo root rather than
inside tests/integration/, which is otherwise where all the real
integration-test setup lives (hass_config_dir, enable_custom_integrations
- see its own conftest.py).
"""

pytest_plugins = "pytest_homeassistant_custom_component"
